"""Capture Level2 data from opening auction start through the first 5 minutes.

The probe records raw Level2 events for later replay/analysis and summarizes
coverage across the auction, the final 10 seconds, and 09:30-09:35.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

from core.data_subscription import DataSubscriptionManager


DEFAULT_KINDS = ("l2quote", "l2order", "l2transaction", "l2orderqueue")
DEFAULT_CAPTURE_START = "09:15:00"
DEFAULT_CAPTURE_END = "09:35:00"
DEFAULT_FINAL_10S_START = "09:24:50"
DEFAULT_FINAL_10S_END = "09:25:00"
DEFAULT_OPEN_5M_START = "09:30:00"
DEFAULT_OPEN_5M_END = "09:35:00"


def normalize_stock_code(value: object) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6) if text.isdigit() else ""


def load_codes(*, csv_path: str = "", codes_text: str = "") -> list[str]:
    codes: list[str] = []
    if codes_text:
        for item in codes_text.replace("，", ",").split(","):
            code = normalize_stock_code(item)
            if code:
                codes.append(code)

    if csv_path:
        path = Path(csv_path)
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames:
                code_column = _find_code_column(reader.fieldnames)
                for row in reader:
                    code = normalize_stock_code(row.get(code_column, ""))
                    if code:
                        codes.append(code)
            else:
                handle.seek(0)
                for row in csv.reader(handle):
                    if row:
                        code = normalize_stock_code(row[0])
                        if code:
                            codes.append(code)

    return sorted(dict.fromkeys(codes))


def _find_code_column(columns: Iterable[str]) -> str:
    candidates = ("股票代码", "代码", "stock_code", "code", "symbol")
    normalized = {str(column).strip().lower(): column for column in columns}
    for candidate in candidates:
        found = normalized.get(candidate.lower())
        if found:
            return found
    return next(iter(columns), "")


def parse_clock(value: str) -> dtime:
    return datetime.strptime(value, "%H:%M:%S").time()


def combine_today(clock: dtime, now: datetime | None = None) -> datetime:
    base = now or datetime.now()
    return datetime.combine(base.date(), clock)


def wait_until(target: datetime, *, label: str) -> None:
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            return
        print(f"WAIT {label} at={target.strftime('%H:%M:%S')} remaining={remaining:.1f}s", flush=True)
        time.sleep(min(remaining, 10.0))


def configure_output(log_file: str) -> None:
    if not log_file:
        return

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8", newline="")

    class _Tee:
        def __init__(self, *streams):
            self._streams = streams

        def write(self, message: str) -> None:
            for stream in self._streams:
                if stream is None:
                    continue
                stream.write(message)
                stream.flush()

        def flush(self) -> None:
            for stream in self._streams:
                if stream is None:
                    continue
                stream.flush()

    stdout = sys.stdout if getattr(sys, "stdout", None) is not None else None
    stderr = sys.stderr if getattr(sys, "stderr", None) is not None else None
    sys.stdout = _Tee(stdout, handle) if stdout is not None else handle  # type: ignore[assignment]
    sys.stderr = _Tee(stderr, handle) if stderr is not None else handle  # type: ignore[assignment]
    print(f"LOG_FILE {log_path}", flush=True)


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="milliseconds")
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "tolist"):
        try:
            return to_jsonable(value.tolist())
        except Exception:
            return str(value)
    return value


class OpeningAuctionL2Recorder:
    """Persist raw Level2 events and build coverage summaries."""

    def __init__(
        self,
        output_dir: Path,
        *,
        capture_start: str = DEFAULT_CAPTURE_START,
        capture_end: str = DEFAULT_CAPTURE_END,
        final_10s_start: str = DEFAULT_FINAL_10S_START,
        final_10s_end: str = DEFAULT_FINAL_10S_END,
        open_5m_start: str = DEFAULT_OPEN_5M_START,
        open_5m_end: str = DEFAULT_OPEN_5M_END,
        thresholds: tuple[float, ...] = (100_000, 300_000, 500_000, 1_000_000, 3_000_000),
    ) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.raw_path = output_dir / "opening_l2_raw.jsonl"
        self.summary_path = output_dir / "opening_l2_summary.csv"
        self.health_path = output_dir / "l2_subscription_health.csv"
        self.schema_path = output_dir / "opening_l2_schema.json"
        self.capture_start = parse_clock(capture_start)
        self.capture_end = parse_clock(capture_end)
        self.final_10s_start = parse_clock(final_10s_start)
        self.final_10s_end = parse_clock(final_10s_end)
        self.open_5m_start = parse_clock(open_5m_start)
        self.open_5m_end = parse_clock(open_5m_end)
        self.thresholds = thresholds
        self._lock = threading.Lock()
        self._raw_handle = self.raw_path.open("a", encoding="utf-8", newline="")
        self._rows: list[dict[str, Any]] = []
        self._schema: dict[str, Counter[str]] = defaultdict(Counter)
        self._closed = False
        self._expected_codes: dict[str, str] = {}
        self._subscription_diagnostics: list[dict[str, Any]] = []

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._raw_handle.close()

    def set_expected_codes(self, codes: Iterable[str], *, mode: str = "dynamic_small_pool") -> None:
        with self._lock:
            for code in codes:
                normalized = normalize_stock_code(code)
                if normalized:
                    self._expected_codes[normalized] = mode

    def set_subscription_diagnostics(self, diagnostics: Iterable[dict[str, Any]]) -> None:
        with self._lock:
            self._subscription_diagnostics = [dict(item) for item in diagnostics]

    def record_many(self, kind: str, mode: str, events_by_code: dict[str, Any]) -> None:
        with self._lock:
            if self._closed:
                return
        for code, payload in events_by_code.items():
            if isinstance(payload, list):
                for event in payload:
                    self.record_event(kind, mode, code, event)
            else:
                self.record_event(kind, mode, code, payload)

    def record_event(self, kind: str, mode: str, code: str, event: Any) -> None:
        event_time = getattr(event, "event_time", None)
        recv_time = getattr(event, "recv_time", None) or datetime.now()
        raw_fields = dict(getattr(event, "raw_xt_fields", {}) or {})
        in_capture_window = self.is_in_capture_window(event_time)
        in_auction = self.is_in_auction(event_time)
        in_final_10s = self.is_in_final_10s(event_time)
        in_open_5m = self.is_in_open_5m(event_time)
        row = {
            "recv_time": recv_time,
            "event_time": event_time,
            "stock": normalize_stock_code(code),
            "kind": kind,
            "subscribe_mode": mode,
            "in_capture_window": in_capture_window,
            "in_auction": in_auction,
            "in_final_10s": in_final_10s,
            "in_open_5m": in_open_5m,
            "phase": self.phase_for_event(event_time),
            "normalized": to_jsonable(event),
            "raw": to_jsonable(raw_fields),
        }
        with self._lock:
            if self._closed:
                return
            self._rows.append(row)
            self._schema[kind].update(str(key) for key in raw_fields.keys())
            self._raw_handle.write(json.dumps(to_jsonable(row), ensure_ascii=False, separators=(",", ":")) + "\n")
            self._raw_handle.flush()

    def is_in_capture_window(self, event_time: datetime | None) -> bool:
        return self._is_in_window(event_time, self.capture_start, self.capture_end)

    def is_in_final_10s(self, event_time: datetime | None) -> bool:
        return self._is_in_window(event_time, self.final_10s_start, self.final_10s_end)

    def is_in_auction(self, event_time: datetime | None) -> bool:
        return self._is_in_window(event_time, self.capture_start, self.final_10s_end)

    def is_in_open_5m(self, event_time: datetime | None) -> bool:
        return self._is_in_window(event_time, self.open_5m_start, self.open_5m_end)

    @staticmethod
    def _is_in_window(event_time: datetime | None, start: dtime, end: dtime) -> bool:
        if event_time is None:
            return False
        clock = event_time.time()
        return start <= clock <= end

    def phase_for_event(self, event_time: datetime | None) -> str:
        if event_time is None:
            return "unknown"
        clock = event_time.time()
        if self.final_10s_start <= clock <= self.final_10s_end:
            return "auction_final_10s"
        if self.capture_start <= clock < self.final_10s_start:
            return "auction_before_final_10s"
        if self.final_10s_end < clock < self.open_5m_start:
            return "pre_open_gap"
        if self.open_5m_start <= clock <= self.open_5m_end:
            return "open_first_5m"
        if self.capture_start <= clock <= self.capture_end:
            return "capture_window_other"
        return "outside_capture_window"

    def write_outputs(self) -> None:
        summary_rows = self.build_summary_rows()
        with self.summary_path.open("w", encoding="utf-8-sig", newline="") as handle:
            fieldnames = self.summary_fieldnames()
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in summary_rows:
                writer.writerow({key: row.get(key, "") for key in fieldnames})

        health_rows = self.build_health_rows(summary_rows)
        with self.health_path.open("w", encoding="utf-8-sig", newline="") as handle:
            fieldnames = self.health_fieldnames()
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in health_rows:
                writer.writerow({key: row.get(key, "") for key in fieldnames})

        schema_payload = {
            kind: [{"field": field, "count": count} for field, count in counter.most_common()]
            for kind, counter in sorted(self._schema.items())
        }
        self.schema_path.write_text(json.dumps(schema_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def build_summary_rows(self) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        with self._lock:
            rows = list(self._rows)
            expected_codes = dict(self._expected_codes)
        for row in rows:
            grouped[(str(row["stock"]), str(row["subscribe_mode"]))].append(row)

        result = []
        for (stock, mode), group_rows in sorted(grouped.items()):
            row: dict[str, Any] = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "stock": stock,
                "l2_subscribe_mode": mode,
                "has_l2_capture": any(item["in_capture_window"] for item in group_rows),
                "has_l2_auction": any(item.get("in_auction") for item in group_rows),
                "has_l2_2450_2500": any(item.get("in_final_10s") for item in group_rows),
                "has_l2_open_5m": any(item.get("in_open_5m") for item in group_rows),
            }
            for kind in DEFAULT_KINDS:
                row[f"{kind}_count_total"] = sum(1 for item in group_rows if item["kind"] == kind)
                row[f"{kind}_count_capture"] = sum(
                    1 for item in group_rows if item["kind"] == kind and item["in_capture_window"]
                )
                row[f"{kind}_count_auction"] = sum(
                    1 for item in group_rows if item["kind"] == kind and item.get("in_auction")
                )
                row[f"{kind}_count_10s"] = sum(
                    1 for item in group_rows if item["kind"] == kind and item.get("in_final_10s")
                )
                row[f"{kind}_count_open_5m"] = sum(
                    1 for item in group_rows if item["kind"] == kind and item.get("in_open_5m")
                )

            self._fill_big_trade_metrics(row, group_rows)
            result.append(row)
        existing = {(str(row["stock"]), str(row["l2_subscribe_mode"])) for row in result}
        for stock, mode in sorted(expected_codes.items()):
            key = (stock, mode)
            if key in existing:
                continue
            row = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "stock": stock,
                "l2_subscribe_mode": mode,
                "has_l2_capture": False,
                "has_l2_auction": False,
                "has_l2_2450_2500": False,
                "has_l2_open_5m": False,
            }
            for kind in DEFAULT_KINDS:
                row[f"{kind}_count_total"] = 0
                row[f"{kind}_count_capture"] = 0
                row[f"{kind}_count_auction"] = 0
                row[f"{kind}_count_10s"] = 0
                row[f"{kind}_count_open_5m"] = 0
            self._fill_big_trade_metrics(row, [])
            result.append(row)
        return result

    def build_health_rows(self, summary_rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        summary_rows = summary_rows if summary_rows is not None else self.build_summary_rows()
        with self._lock:
            diagnostics = [dict(item) for item in self._subscription_diagnostics]
            rows = list(self._rows)
            expected_codes = dict(self._expected_codes)

        diag_by_key = {
            (normalize_stock_code(item.get("stock")), str(item.get("kind") or "").strip().lower()): item
            for item in diagnostics
        }
        first_event: dict[tuple[str, str], datetime] = {}
        last_event: dict[tuple[str, str], datetime] = {}
        for row in rows:
            stock = str(row.get("stock") or "")
            kind = str(row.get("kind") or "")
            event_time = row.get("event_time") or row.get("recv_time")
            if not isinstance(event_time, datetime):
                continue
            key = (stock, kind)
            if key not in first_event or event_time < first_event[key]:
                first_event[key] = event_time
            if key not in last_event or event_time > last_event[key]:
                last_event[key] = event_time

        summary_by_stock = {str(row.get("stock") or ""): row for row in summary_rows}
        stocks = sorted(set(expected_codes) | set(summary_by_stock))
        result = []
        for stock in stocks:
            summary = summary_by_stock.get(stock, {})
            order_diag = diag_by_key.get((stock, "l2order"), {})
            tx_diag = diag_by_key.get((stock, "l2transaction"), {})
            order_count = int(summary.get("l2order_count_total") or 0)
            tx_count = int(summary.get("l2transaction_count_total") or 0)
            order_status = str(order_diag.get("status") or "")
            tx_status = str(tx_diag.get("status") or "")
            has_events = order_count > 0 or tx_count > 0
            invalid_sub_id = order_status == "INVALID_SUB_ID" or tx_status == "INVALID_SUB_ID"
            subscribe_exception = order_status == "SUBSCRIBE_EXCEPTION" or tx_status == "SUBSCRIBE_EXCEPTION"
            xt_unavailable = order_status == "XT_UNAVAILABLE" or tx_status == "XT_UNAVAILABLE"
            if subscribe_exception:
                status = "SUBSCRIBE_EXCEPTION"
            elif xt_unavailable:
                status = "XT_UNAVAILABLE"
            elif invalid_sub_id:
                status = "INVALID_SUB_ID"
            elif has_events:
                status = "SUBSCRIBED_WITH_EVENTS"
            else:
                status = "SUBSCRIBED_NO_EVENTS"
            result.append(
                {
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "stock": stock,
                    "l2_subscribe_mode": summary.get("l2_subscribe_mode") or expected_codes.get(stock, ""),
                    "status": status,
                    "l2order_sub_id": order_diag.get("sub_id", ""),
                    "l2transaction_sub_id": tx_diag.get("sub_id", ""),
                    "l2order_subscribe_start": self._format_dt(order_diag.get("subscribe_start")),
                    "l2transaction_subscribe_start": self._format_dt(tx_diag.get("subscribe_start")),
                    "l2order_duration_ms": order_diag.get("duration_ms", ""),
                    "l2transaction_duration_ms": tx_diag.get("duration_ms", ""),
                    "first_l2order_time": self._format_dt(first_event.get((stock, "l2order"))),
                    "first_l2transaction_time": self._format_dt(first_event.get((stock, "l2transaction"))),
                    "last_l2_event_time": self._format_dt(
                        max(
                            [
                                value
                                for value in (
                                    last_event.get((stock, "l2order")),
                                    last_event.get((stock, "l2transaction")),
                                )
                                if value is not None
                            ],
                            default=None,
                        )
                    ),
                    "l2order_count": order_count,
                    "l2transaction_count": tx_count,
                    "l2order_error": order_diag.get("error", ""),
                    "l2transaction_error": tx_diag.get("error", ""),
                }
            )
        return result

    def _fill_big_trade_metrics(self, row: dict[str, Any], group_rows: list[dict[str, Any]]) -> None:
        trade_events = [
            item["normalized"]
            for item in group_rows
            if item["kind"] == "l2transaction" and item.get("in_final_10s")
        ]
        order_events = [
            item["normalized"]
            for item in group_rows
            if item["kind"] == "l2order" and item.get("in_final_10s")
        ]
        for threshold in self.thresholds:
            key = int(threshold / 10_000)
            row[f"big_trade_amount_{key}w"] = round(
                sum(float(item.get("amount", 0) or 0) for item in trade_events if float(item.get("amount", 0) or 0) >= threshold),
                3,
            )

        buy_amount = 0.0
        sell_amount = 0.0
        for item in trade_events:
            amount = float(item.get("amount", 0) or 0)
            side = infer_side_from_mapping(item)
            if side == "BUY":
                buy_amount += amount
            elif side == "SELL":
                sell_amount += amount

        buy_order_amount = 0.0
        sell_order_amount = 0.0
        cancel_buy_amount = 0.0
        cancel_sell_amount = 0.0
        for item in order_events:
            amount = float(item.get("amount", 0) or 0)
            side = infer_side_from_mapping(item)
            if side == "BUY":
                buy_order_amount += amount
            elif side == "SELL":
                sell_order_amount += amount
            elif side == "CANCEL_BUY":
                cancel_buy_amount += amount
            elif side == "CANCEL_SELL":
                cancel_sell_amount += amount

        row.update(
            {
                "big_buy_amount_10s": round(buy_amount, 3),
                "big_sell_amount_10s": round(sell_amount, 3),
                "big_trade_imbalance_10s": round(buy_amount - sell_amount, 3),
                "big_buy_order_amount_10s": round(buy_order_amount, 3),
                "big_sell_order_amount_10s": round(sell_order_amount, 3),
                "big_order_imbalance_10s": round(buy_order_amount - sell_order_amount, 3),
                "cancel_buy_order_amount_10s": round(cancel_buy_amount, 3),
                "cancel_sell_order_amount_10s": round(cancel_sell_amount, 3),
            }
        )

    @staticmethod
    def summary_fieldnames() -> list[str]:
        fields = [
            "date",
            "stock",
            "l2_subscribe_mode",
            "has_l2_capture",
            "has_l2_auction",
            "has_l2_2450_2500",
            "has_l2_open_5m",
        ]
        for kind in DEFAULT_KINDS:
            fields.extend(
                [
                    f"{kind}_count_total",
                    f"{kind}_count_capture",
                    f"{kind}_count_auction",
                    f"{kind}_count_10s",
                    f"{kind}_count_open_5m",
                ]
            )
        fields.extend(
            [
                "big_trade_amount_10w",
                "big_trade_amount_30w",
                "big_trade_amount_50w",
                "big_trade_amount_100w",
                "big_trade_amount_300w",
                "big_buy_amount_10s",
                "big_sell_amount_10s",
                "big_trade_imbalance_10s",
                "big_buy_order_amount_10s",
                "big_sell_order_amount_10s",
                "big_order_imbalance_10s",
                "cancel_buy_order_amount_10s",
                "cancel_sell_order_amount_10s",
            ]
        )
        return fields

    @staticmethod
    def health_fieldnames() -> list[str]:
        return [
            "date",
            "stock",
            "l2_subscribe_mode",
            "status",
            "l2order_sub_id",
            "l2transaction_sub_id",
            "l2order_subscribe_start",
            "l2transaction_subscribe_start",
            "l2order_duration_ms",
            "l2transaction_duration_ms",
            "first_l2order_time",
            "first_l2transaction_time",
            "last_l2_event_time",
            "l2order_count",
            "l2transaction_count",
            "l2order_error",
            "l2transaction_error",
        ]

    @staticmethod
    def _format_dt(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat(sep=" ", timespec="milliseconds")
        return ""


def infer_side_from_mapping(item: dict[str, Any]) -> str:
    side = str(item.get("side", "") or "").strip().upper()
    if side in {"BUY", "B", "1", "SELL", "S", "2", "CANCEL_BUY", "CANCEL_SELL"}:
        if side == "B" or side == "1":
            return "BUY"
        if side == "S" or side == "2":
            return "SELL"
        return side
    trade_flag = item.get("trade_flag")
    entrust_direction = item.get("entrust_direction")
    if trade_flag == 1 or entrust_direction == 1:
        return "BUY"
    if trade_flag == 2 or entrust_direction == 2:
        return "SELL"
    if entrust_direction == 3:
        return "CANCEL_BUY"
    if entrust_direction == 4:
        return "CANCEL_SELL"
    return side


def run_probe(args: argparse.Namespace) -> None:
    early_codes = load_codes(csv_path=args.early_pool, codes_text=args.early_codes)
    delayed_codes = load_codes(csv_path=args.delayed_pool, codes_text=args.delayed_codes)
    if not early_codes and not delayed_codes:
        raise SystemExit("No stock codes provided. Use --early-codes/--early-pool or --delayed-codes/--delayed-pool.")

    output_dir = Path(args.output_dir)
    if not args.output_dir:
        output_dir = Path("data/probe/opening_auction_l2") / datetime.now().strftime("%Y%m%d_%H%M%S")
    recorder = OpeningAuctionL2Recorder(
        output_dir,
        capture_start=args.capture_start,
        capture_end=args.capture_end,
        final_10s_start=args.final_10s_start,
        final_10s_end=args.final_10s_end,
        open_5m_start=args.open_5m_start,
        open_5m_end=args.open_5m_end,
    )
    manager = DataSubscriptionManager()
    code_modes = {code: "early" for code in early_codes}
    code_modes.update({code: "delayed" for code in delayed_codes if code not in code_modes})

    def mode_for(code: str) -> str:
        return code_modes.get(normalize_stock_code(code), "unknown")

    manager.set_l2_quote_callback(lambda events: record_with_modes(recorder, "l2quote", events, mode_for))
    manager.set_l2_order_callback(lambda events: record_with_modes(recorder, "l2order", events, mode_for))
    manager.set_l2_transaction_callback(lambda events: record_with_modes(recorder, "l2transaction", events, mode_for))
    manager.set_l2_orderqueue_callback(lambda events: record_with_modes(recorder, "l2orderqueue", events, mode_for))

    print(
        "OPENING_AUCTION_L2_PROBE start "
        f"early={early_codes} delayed={delayed_codes} kinds={list(args.kinds)} output={output_dir}",
        flush=True,
    )
    threading.Thread(target=manager.start, daemon=True).start()

    now = datetime.now()
    early_at = combine_today(parse_clock(args.early_subscribe_at), now)
    delayed_at = combine_today(parse_clock(args.delayed_subscribe_at), now)
    stop_at = combine_today(parse_clock(args.stop_at), now)
    if stop_at <= now:
        stop_at += timedelta(days=1)

    try:
        if early_codes:
            if not args.immediate:
                wait_until(early_at, label="early_subscribe")
            print(f"SUBSCRIBE mode=early codes={early_codes} kinds={list(args.kinds)}", flush=True)
            manager.subscribe_l2_stocks(early_codes, kinds=list(args.kinds))

        if delayed_codes:
            if not args.immediate:
                wait_until(delayed_at, label="delayed_subscribe")
            print(f"SUBSCRIBE mode=delayed codes={delayed_codes} kinds={list(args.kinds)}", flush=True)
            manager.subscribe_l2_stocks(delayed_codes, kinds=list(args.kinds))

        while args.immediate is False and datetime.now() < stop_at:
            time.sleep(5)
            recorder.write_outputs()
            print_status(recorder)
        if args.immediate:
            time.sleep(max(0.0, float(args.seconds)))
    except KeyboardInterrupt:
        print("INTERRUPTED", flush=True)
    finally:
        recorder.write_outputs()
        recorder.close()
        manager.stop()
        print(f"RAW_PATH {recorder.raw_path}", flush=True)
        print(f"SUMMARY_PATH {recorder.summary_path}", flush=True)
        print(f"SCHEMA_PATH {recorder.schema_path}", flush=True)


def record_with_modes(
    recorder: OpeningAuctionL2Recorder,
    kind: str,
    events_by_code: dict[str, Any],
    mode_for,
) -> None:
    grouped: dict[str, dict[str, Any]] = defaultdict(dict)
    for code, payload in events_by_code.items():
        grouped[mode_for(code)][code] = payload
    for mode, payload in grouped.items():
        recorder.record_many(kind, mode, payload)


def print_status(recorder: OpeningAuctionL2Recorder) -> None:
    rows = recorder.build_summary_rows()
    capture = sum(1 for row in rows if row.get("has_l2_capture"))
    covered = sum(1 for row in rows if row.get("has_l2_2450_2500"))
    open_5m = sum(1 for row in rows if row.get("has_l2_open_5m"))
    print(f"PROGRESS rows={len(rows)} capture={capture} covered_2450_2500={covered} open_5m={open_5m}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture opening-auction-to-open-first-5m Level2 data.")
    parser.add_argument("--early-pool", default="", help="CSV file for stocks subscribed from auction start.")
    parser.add_argument("--early-codes", default="", help="Comma separated stocks subscribed from auction start.")
    parser.add_argument("--delayed-pool", default="", help="CSV file for control stocks subscribed after 09:25:00.")
    parser.add_argument("--delayed-codes", default="", help="Comma separated control stocks subscribed after 09:25:00.")
    parser.add_argument("--output-dir", default="", help="Output directory. Default: data/probe/opening_auction_l2/YYYYMMDD_HHMMSS")
    parser.add_argument("--early-subscribe-at", default="09:15:00")
    parser.add_argument("--delayed-subscribe-at", default="09:25:05")
    parser.add_argument("--capture-start", default=DEFAULT_CAPTURE_START)
    parser.add_argument("--capture-end", default=DEFAULT_CAPTURE_END)
    parser.add_argument("--final-10s-start", default=DEFAULT_FINAL_10S_START)
    parser.add_argument("--final-10s-end", default=DEFAULT_FINAL_10S_END)
    parser.add_argument("--open-5m-start", default=DEFAULT_OPEN_5M_START)
    parser.add_argument("--open-5m-end", default=DEFAULT_OPEN_5M_END)
    parser.add_argument("--stop-at", default="09:35:00")
    parser.add_argument("--kinds", nargs="+", default=list(DEFAULT_KINDS), choices=list(DEFAULT_KINDS))
    parser.add_argument("--immediate", action="store_true", help="Subscribe immediately; useful for smoke tests outside auction time.")
    parser.add_argument("--seconds", type=float, default=10.0, help="Runtime seconds when --immediate is used.")
    parser.add_argument("--log-file", default="", help="Optional log file path. Useful when launched from pythonw.exe.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    configure_output(args.log_file)
    run_probe(args)


if __name__ == "__main__":
    main()
