"""Replay OpeningAuctionL2Probe raw data into auction-attitude decisions.

This is an offline review tool. It reads probe JSONL artifacts and writes
CSV/Markdown summaries without connecting to QMT or placing orders.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategies.opening_auction_attitude import (
    AuctionL1Window,
    AuctionL2Window,
    AuctionPricePoint,
    AuctionScoreConfig,
    OpenVerifyConfig,
    OpenVerifyPoint,
    OpenVerifyWindow,
    evaluate_auction_attitude,
    evaluate_open_behavior,
)


DEFAULT_RAW_NAME = "opening_l2_raw.jsonl"
DEFAULT_NAME_POOL = "data/stock_pools/current/main_seal_follow_pool.csv"
DEFAULT_NAME_POOL_ROOT = "data/stock_pools"


def exchange_for(stock: str) -> str:
    text = str(stock or "").strip()
    if text.startswith("6"):
        return "SH"
    if text.startswith(("0", "2", "3")):
        return "SZ"
    return "UNKNOWN"


def parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value)
    for candidate in (text, text.replace("T", " ")):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def resolve_raw_path(input_dir: str, raw_path: str) -> Path:
    if raw_path:
        return Path(raw_path)
    if not input_dir:
        raise SystemExit("Use --input-dir or --raw.")
    return Path(input_dir) / DEFAULT_RAW_NAME


def normalize_stock_code(value: object) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6) if text.isdigit() else ""


def load_stock_names(pool_path: str) -> dict[str, str]:
    if not pool_path:
        return {}
    path = Path(pool_path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return {}
        code_column = _find_column(reader.fieldnames, ("股票代码", "代码", "stock_code", "code", "symbol"))
        name_column = _find_column(reader.fieldnames, ("名称", "股票名称", "name", "stock_name"))
        if not code_column or not name_column:
            return {}
        result: dict[str, str] = {}
        for row in reader:
            code = normalize_stock_code(row.get(code_column))
            name = str(row.get(name_column) or "").strip()
            if code and name:
                result[code] = name
        return result


def load_stock_names_from_tree(root_path: str) -> dict[str, str]:
    if not root_path:
        return {}
    root = Path(root_path)
    if not root.exists():
        return {}
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*.csv")):
        result.update(load_stock_names(str(path)))
    return result


def _find_column(columns: list[str], candidates: tuple[str, ...]) -> str:
    normalized = {str(column).strip().lower(): column for column in columns}
    for candidate in candidates:
        found = normalized.get(candidate.lower())
        if found:
            return found
    return ""


class ReplayAggregator:
    """Aggregate probe rows into L1/L2 windows per stock."""

    def __init__(self, config: AuctionScoreConfig | None = None, stock_names: dict[str, str] | None = None) -> None:
        self.config = config or AuctionScoreConfig()
        self.open_config = OpenVerifyConfig()
        self.quote_volume_unit = 100.0
        self.stock_names = stock_names or {}
        self.price_points: dict[str, list[AuctionPricePoint]] = defaultdict(list)
        self.open_points: dict[str, list[OpenVerifyPoint]] = defaultdict(list)
        self.pre_close: dict[str, float] = {}
        self.l2_stats: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {
                "l2quote_count": 0,
                "l2order_count": 0,
                "l2transaction_count": 0,
                "l2orderqueue_count": 0,
                "big_buy_order_amount": 0.0,
                "big_sell_order_amount": 0.0,
                "cancel_buy_order_amount": 0.0,
                "cancel_sell_order_amount": 0.0,
                "big_buy_trade_amount": 0.0,
                "big_sell_trade_amount": 0.0,
            }
        )
        self.open_stats: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {
                "open_l2transaction_count": 0,
                "open_buy_trade_amount": 0.0,
                "open_sell_trade_amount": 0.0,
            }
        )
        self.malformed_lines = 0
        self.rows_seen = 0
        self.rows_in_window = 0

    def add_json_line(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            self.malformed_lines += 1
            return
        self.add_row(row)

    def add_row(self, row: dict[str, Any]) -> None:
        self.rows_seen += 1
        event_time = parse_dt(row.get("event_time"))
        in_auction_window = self._in_auction_window(event_time)
        in_open_window = self._in_open_window(event_time)
        if not in_auction_window and not in_open_window:
            return
        stock = str(row.get("stock") or "").strip()
        if not stock:
            return
        kind = str(row.get("kind") or "").strip()
        normalized = row.get("normalized") if isinstance(row.get("normalized"), dict) else {}
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        self.rows_in_window += 1

        if kind == "l2quote" and in_auction_window:
            self._add_l2quote(stock, event_time, normalized, raw)
        elif kind == "l2quote" and in_open_window:
            self._add_open_l2quote(stock, event_time, normalized, raw)
        elif kind == "l2order" and in_auction_window:
            self._add_l2order(stock, normalized)
        elif kind == "l2transaction" and in_auction_window:
            self._add_l2transaction(stock, normalized)
        elif kind == "l2transaction" and in_open_window:
            self._add_open_l2transaction(stock, normalized)
        elif kind == "l2orderqueue" and in_auction_window:
            self.l2_stats[stock]["l2orderqueue_count"] = int(self.l2_stats[stock]["l2orderqueue_count"]) + 1

    def decisions(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for stock in sorted(set(self.price_points) | set(self.l2_stats) | set(self.open_points) | set(self.open_stats)):
            l1 = AuctionL1Window(
                symbol=stock,
                pre_close=float(self.pre_close.get(stock, 0.0) or 0.0),
                points=sorted(
                    self.price_points.get(stock, []),
                    key=lambda item: item.event_time or datetime.min,
                ),
            )
            stats = self.l2_stats[stock]
            l2 = AuctionL2Window(
                symbol=stock,
                l2quote_count=int(stats["l2quote_count"]),
                l2order_count=int(stats["l2order_count"]),
                l2transaction_count=int(stats["l2transaction_count"]),
                l2orderqueue_count=int(stats["l2orderqueue_count"]),
                big_buy_order_amount=float(stats["big_buy_order_amount"]),
                big_sell_order_amount=float(stats["big_sell_order_amount"]),
                cancel_buy_order_amount=float(stats["cancel_buy_order_amount"]),
                cancel_sell_order_amount=float(stats["cancel_sell_order_amount"]),
                big_buy_trade_amount=float(stats["big_buy_trade_amount"]),
                big_sell_trade_amount=float(stats["big_sell_trade_amount"]),
            )
            decision = evaluate_auction_attitude(l1, l2, self.config)
            evidence = decision.evidence or {}
            open_stats = self.open_stats[stock]
            open_decision = evaluate_open_behavior(
                OpenVerifyWindow(
                    symbol=stock,
                    auction_label=decision.auction_label,
                    auction_final_price=float(evidence.get("auction_final_price", 0.0) or 0.0),
                    points=sorted(
                        self.open_points.get(stock, []),
                        key=lambda item: item.event_time or datetime.min,
                    ),
                    buy_trade_amount=float(open_stats["open_buy_trade_amount"]),
                    sell_trade_amount=float(open_stats["open_sell_trade_amount"]),
                ),
                self.open_config,
            )
            open_evidence = open_decision.evidence or {}
            rows.append(
                {
                    "stock": stock,
                    "stock_name": self.stock_names.get(stock, ""),
                    "exchange": exchange_for(stock),
                    "auction_label": decision.auction_label,
                    "auction_speed_score": decision.auction_speed_score,
                    "auction_attitude_score": decision.auction_attitude_score,
                    "reason": decision.reason,
                    "open_verify_path": open_decision.open_verify_path,
                    "open_verify_score": open_decision.open_verify_score,
                    "open_verify_reason": open_decision.reason,
                    "price_point_count": len(l1.points),
                    "open_point_count": len(self.open_points.get(stock, [])),
                    "pre_close": l1.pre_close,
                    "auction_low_price": evidence.get("auction_low_price", 0.0),
                    "auction_low_time": _format_dt(evidence.get("auction_low_time")),
                    "auction_final_price": evidence.get("auction_final_price", 0.0),
                    "auction_final_time": _format_dt(evidence.get("auction_final_time")),
                    "auction_high_price": evidence.get("auction_high_price", 0.0),
                    "final_gap_pct": evidence.get("final_gap_pct", 0.0),
                    "low_to_final_lift_pct": evidence.get("low_to_final_lift_pct", 0.0),
                    "amount_at_low": evidence.get("amount_at_low", 0.0),
                    "amount_at_final": evidence.get("amount_at_final", 0.0),
                    "amount_source_at_low": evidence.get("amount_source_at_low", ""),
                    "amount_source_at_final": evidence.get("amount_source_at_final", ""),
                    "amount_is_cumulative": evidence.get("amount_is_cumulative", True),
                    "matched_volume_at_low": evidence.get("matched_volume_at_low", 0.0),
                    "matched_volume_at_final": evidence.get("matched_volume_at_final", 0.0),
                    "unmatched_buy_volume_at_final": evidence.get("unmatched_buy_volume_at_final", 0.0),
                    "unmatched_sell_volume_at_final": evidence.get("unmatched_sell_volume_at_final", 0.0),
                    "unmatched_buy_amount_at_final": evidence.get("unmatched_buy_amount_at_final", 0.0),
                    "unmatched_sell_amount_at_final": evidence.get("unmatched_sell_amount_at_final", 0.0),
                    "unmatched_amount_imbalance_at_final": evidence.get("unmatched_amount_imbalance_at_final", 0.0),
                    "has_unmatched_sell_pressure": evidence.get("has_unmatched_sell_pressure", False),
                    "low_to_final_amount_delta": evidence.get("low_to_final_amount_delta", 0.0),
                    "low_to_final_amount_ratio": evidence.get("low_to_final_amount_ratio", 0.0),
                    "final_near_high": evidence.get("final_near_high", False),
                    "l2quote_count": l2.l2quote_count,
                    "l2order_count": l2.l2order_count,
                    "l2transaction_count": l2.l2transaction_count,
                    "l2orderqueue_count": l2.l2orderqueue_count,
                    "big_buy_order_amount": l2.big_buy_order_amount,
                    "big_sell_order_amount": l2.big_sell_order_amount,
                    "big_order_imbalance": evidence.get("big_order_imbalance", 0.0),
                    "big_buy_trade_amount": l2.big_buy_trade_amount,
                    "big_sell_trade_amount": l2.big_sell_trade_amount,
                    "big_trade_imbalance": evidence.get("big_trade_imbalance", 0.0),
                    "has_order_confirmation": evidence.get("has_order_confirmation", False),
                    "has_trade_data": evidence.get("has_trade_data", False),
                    "has_trade_confirmation": evidence.get("has_trade_confirmation", False),
                    "open_price": open_evidence.get("open_price", 0.0),
                    "open_time": _format_dt(open_evidence.get("open_time")),
                    "open_high_price": open_evidence.get("high_price", 0.0),
                    "open_high_time": _format_dt(open_evidence.get("high_time")),
                    "open_low_price": open_evidence.get("low_price", 0.0),
                    "open_low_time": _format_dt(open_evidence.get("low_time")),
                    "open_final_price": open_evidence.get("final_price", 0.0),
                    "open_final_time": _format_dt(open_evidence.get("final_time")),
                    "open_max_gain_from_open_pct": open_evidence.get("max_gain_from_open_pct", 0.0),
                    "open_max_drawdown_from_open_pct": open_evidence.get("max_drawdown_from_open_pct", 0.0),
                    "open_final_return_pct": open_evidence.get("final_return_pct", 0.0),
                    "open_buy_trade_amount": float(open_stats["open_buy_trade_amount"]),
                    "open_sell_trade_amount": float(open_stats["open_sell_trade_amount"]),
                    "open_buy_trade_ratio": open_evidence.get("buy_trade_ratio", 0.0),
                    "open_sell_trade_ratio": open_evidence.get("sell_trade_ratio", 0.0),
                    "open_has_buy_confirmation": open_evidence.get("has_buy_confirmation", False),
                    "open_has_sell_pressure": open_evidence.get("has_sell_pressure", False),
                    "open_l2transaction_count": int(open_stats["open_l2transaction_count"]),
                }
            )
        return rows

    def _add_l2quote(self, stock: str, event_time: datetime | None, normalized: dict[str, Any], raw: dict[str, Any]) -> None:
        self.l2_stats[stock]["l2quote_count"] = int(self.l2_stats[stock]["l2quote_count"]) + 1
        price = _to_float(normalized.get("last_price") or raw.get("lastPrice"))
        quote_amount = _auction_quote_amount(raw, fallback_price=price, quote_volume_unit=self.quote_volume_unit)
        if quote_amount["price"] > 0:
            price = quote_amount["price"]
        amount = quote_amount["matched_amount"]
        pre_close = _to_float(normalized.get("pre_close") or raw.get("lastClose"))
        if pre_close > 0:
            self.pre_close[stock] = pre_close
        if price > 0:
            self.price_points[stock].append(
                AuctionPricePoint(
                    event_time=event_time,
                    price=price,
                    matched_amount=max(0.0, amount),
                    matched_volume=quote_amount["matched_volume"],
                    unmatched_buy_volume=quote_amount["unmatched_buy_volume"],
                    unmatched_sell_volume=quote_amount["unmatched_sell_volume"],
                    unmatched_buy_amount=quote_amount["unmatched_buy_amount"],
                    unmatched_sell_amount=quote_amount["unmatched_sell_amount"],
                    amount_source=quote_amount["amount_source"],
                )
            )

    def _add_open_l2quote(
        self,
        stock: str,
        event_time: datetime | None,
        normalized: dict[str, Any],
        raw: dict[str, Any],
    ) -> None:
        price = _to_float(normalized.get("last_price") or raw.get("lastPrice"))
        amount = _to_float(normalized.get("amount") or raw.get("amount"))
        volume = _to_float(normalized.get("volume") or raw.get("pvolume") or raw.get("volume"))
        if price > 0:
            self.open_points[stock].append(
                OpenVerifyPoint(
                    event_time=event_time,
                    price=price,
                    amount=max(0.0, amount),
                    volume=max(0.0, volume),
                )
            )

    def _add_l2order(self, stock: str, normalized: dict[str, Any]) -> None:
        self.l2_stats[stock]["l2order_count"] = int(self.l2_stats[stock]["l2order_count"]) + 1
        side = str(normalized.get("side") or "").strip().upper()
        amount = _to_float(normalized.get("amount"))
        if amount <= 0:
            amount = _to_float(normalized.get("price")) * _to_float(normalized.get("volume"))
        if side == "BUY":
            self.l2_stats[stock]["big_buy_order_amount"] = float(self.l2_stats[stock]["big_buy_order_amount"]) + amount
        elif side == "SELL":
            self.l2_stats[stock]["big_sell_order_amount"] = float(self.l2_stats[stock]["big_sell_order_amount"]) + amount
        elif side == "CANCEL_BUY":
            self.l2_stats[stock]["cancel_buy_order_amount"] = float(self.l2_stats[stock]["cancel_buy_order_amount"]) + amount
        elif side == "CANCEL_SELL":
            self.l2_stats[stock]["cancel_sell_order_amount"] = float(self.l2_stats[stock]["cancel_sell_order_amount"]) + amount

    def _add_l2transaction(self, stock: str, normalized: dict[str, Any]) -> None:
        self.l2_stats[stock]["l2transaction_count"] = int(self.l2_stats[stock]["l2transaction_count"]) + 1
        side = _trade_side(normalized)
        amount = _to_float(normalized.get("amount"))
        if amount <= 0:
            amount = _to_float(normalized.get("price")) * _to_float(normalized.get("volume"))
        if side == "BUY":
            self.l2_stats[stock]["big_buy_trade_amount"] = float(self.l2_stats[stock]["big_buy_trade_amount"]) + amount
        elif side == "SELL":
            self.l2_stats[stock]["big_sell_trade_amount"] = float(self.l2_stats[stock]["big_sell_trade_amount"]) + amount

    def _add_open_l2transaction(self, stock: str, normalized: dict[str, Any]) -> None:
        self.open_stats[stock]["open_l2transaction_count"] = int(self.open_stats[stock]["open_l2transaction_count"]) + 1
        side = _trade_side(normalized)
        amount = _to_float(normalized.get("amount"))
        if amount <= 0:
            amount = _to_float(normalized.get("price")) * _to_float(normalized.get("volume"))
        if side == "BUY":
            self.open_stats[stock]["open_buy_trade_amount"] = (
                float(self.open_stats[stock]["open_buy_trade_amount"]) + amount
            )
        elif side == "SELL":
            self.open_stats[stock]["open_sell_trade_amount"] = (
                float(self.open_stats[stock]["open_sell_trade_amount"]) + amount
            )

    def _in_auction_window(self, event_time: datetime | None) -> bool:
        if event_time is None:
            return True
        clock = event_time.time()
        return self.config.window_start <= clock <= self.config.window_end

    def _in_open_window(self, event_time: datetime | None) -> bool:
        if event_time is None:
            return False
        clock = event_time.time()
        return self.open_config.window_start <= clock <= self.open_config.window_end


def replay_raw_jsonl(
    raw_path: Path,
    *,
    config: AuctionScoreConfig | None = None,
    stock_names: dict[str, str] | None = None,
    max_lines: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    aggregator = ReplayAggregator(config, stock_names=stock_names)
    with raw_path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if max_lines and line_no > max_lines:
                break
            aggregator.add_json_line(line)
    rows = aggregator.decisions()
    summary = {
        "raw_path": str(raw_path),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows_seen": aggregator.rows_seen,
        "rows_in_window": aggregator.rows_in_window,
        "malformed_lines": aggregator.malformed_lines,
        "stock_count": len(rows),
        "score_config": {
            "window_start": aggregator.config.window_start.isoformat(),
            "window_end": aggregator.config.window_end.isoformat(),
            "min_low_to_final_lift_pct": aggregator.config.min_low_to_final_lift_pct,
            "min_final_gap_pct": aggregator.config.min_final_gap_pct,
            "min_money_lift_ratio": aggregator.config.min_money_lift_ratio,
            "strong_money_lift_ratio": aggregator.config.strong_money_lift_ratio,
            "close_to_high_tolerance_pct": aggregator.config.close_to_high_tolerance_pct,
            "big_buy_ratio_threshold": aggregator.config.big_buy_ratio_threshold,
            "sell_pressure_ratio_threshold": aggregator.config.sell_pressure_ratio_threshold,
        },
        "open_verify_config": {
            "window_start": aggregator.open_config.window_start.isoformat(),
            "window_end": aggregator.open_config.window_end.isoformat(),
            "direct_check_end_sec": aggregator.open_config.direct_check_end_sec,
            "min_direct_pull_pct": aggregator.open_config.min_direct_pull_pct,
            "max_direct_drawdown_pct": aggregator.open_config.max_direct_drawdown_pct,
            "min_wash_dip_pct": aggregator.open_config.min_wash_dip_pct,
            "max_wash_drawdown_pct": aggregator.open_config.max_wash_drawdown_pct,
            "breakdown_pct": aggregator.open_config.breakdown_pct,
        },
    }
    return rows, summary


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = replay_fieldnames()
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def write_markdown(rows: list[dict[str, Any]], summary: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Opening Auction Attitude Replay",
        "",
        f"- raw_path: `{summary['raw_path']}`",
        f"- generated_at: `{summary['generated_at']}`",
        f"- rows_seen: `{summary['rows_seen']}`",
        f"- rows_in_window: `{summary['rows_in_window']}`",
        f"- malformed_lines: `{summary['malformed_lines']}`",
        f"- stock_count: `{summary['stock_count']}`",
        "",
        "## Score Config",
        "",
    ]
    for key, value in (summary.get("score_config") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Open Verify Config", ""])
    for key, value in (summary.get("open_verify_config") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(
        [
            "",
            "## Decisions",
            "",
            "| stock | name | exch | label | open_path | reason | open_reason | low_time | final_time | lift_pct | amount_ratio | open_gain | open_drawdown | order_buy | order_sell | trade_buy | trade_sell | open_buy | open_sell |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in rows:
        lines.append(
            "| {stock} | {name} | {exchange} | {label} | {open_path} | {reason} | {open_reason} | {low_time} | {final_time} | {lift:.4f} | {ratio:.4f} | {open_gain:.4f} | {open_drawdown:.4f} | {order_buy:.2f} | {order_sell:.2f} | {trade_buy:.2f} | {trade_sell:.2f} | {open_buy:.2f} | {open_sell:.2f} |".format(
                stock=row["stock"],
                name=row.get("stock_name", ""),
                exchange=row.get("exchange", ""),
                label=row["auction_label"],
                open_path=row.get("open_verify_path", ""),
                reason=row["reason"],
                open_reason=row.get("open_verify_reason", ""),
                low_time=_clock_text(row.get("auction_low_time", "")),
                final_time=_clock_text(row.get("auction_final_time", "")),
                lift=float(row["low_to_final_lift_pct"] or 0.0),
                ratio=float(row["low_to_final_amount_ratio"] or 0.0),
                open_gain=float(row.get("open_max_gain_from_open_pct", 0.0) or 0.0),
                open_drawdown=float(row.get("open_max_drawdown_from_open_pct", 0.0) or 0.0),
                order_buy=float(row.get("big_buy_order_amount", 0.0) or 0.0),
                order_sell=float(row.get("big_sell_order_amount", 0.0) or 0.0),
                trade_buy=float(row.get("big_buy_trade_amount", 0.0) or 0.0),
                trade_sell=float(row.get("big_sell_trade_amount", 0.0) or 0.0),
                open_buy=float(row.get("open_buy_trade_amount", 0.0) or 0.0),
                open_sell=float(row.get("open_sell_trade_amount", 0.0) or 0.0),
            )
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8-sig")


def replay_fieldnames() -> list[str]:
    return [
        "stock",
        "stock_name",
        "exchange",
        "auction_label",
        "auction_speed_score",
        "auction_attitude_score",
        "reason",
        "open_verify_path",
        "open_verify_score",
        "open_verify_reason",
        "price_point_count",
        "open_point_count",
        "pre_close",
        "auction_low_price",
        "auction_low_time",
        "auction_final_price",
        "auction_final_time",
        "auction_high_price",
        "final_gap_pct",
        "low_to_final_lift_pct",
        "amount_at_low",
        "amount_at_final",
        "amount_source_at_low",
        "amount_source_at_final",
        "amount_is_cumulative",
        "matched_volume_at_low",
        "matched_volume_at_final",
        "unmatched_buy_volume_at_final",
        "unmatched_sell_volume_at_final",
        "unmatched_buy_amount_at_final",
        "unmatched_sell_amount_at_final",
        "unmatched_amount_imbalance_at_final",
        "has_unmatched_sell_pressure",
        "low_to_final_amount_delta",
        "low_to_final_amount_ratio",
        "final_near_high",
        "l2quote_count",
        "l2order_count",
        "l2transaction_count",
        "l2orderqueue_count",
        "big_buy_order_amount",
        "big_sell_order_amount",
        "big_order_imbalance",
        "big_buy_trade_amount",
        "big_sell_trade_amount",
        "big_trade_imbalance",
        "has_order_confirmation",
        "has_trade_data",
        "has_trade_confirmation",
        "open_price",
        "open_time",
        "open_high_price",
        "open_high_time",
        "open_low_price",
        "open_low_time",
        "open_final_price",
        "open_final_time",
        "open_max_gain_from_open_pct",
        "open_max_drawdown_from_open_pct",
        "open_final_return_pct",
        "open_buy_trade_amount",
        "open_sell_trade_amount",
        "open_buy_trade_ratio",
        "open_sell_trade_ratio",
        "open_has_buy_confirmation",
        "open_has_sell_pressure",
        "open_l2transaction_count",
    ]


def run_replay(args: argparse.Namespace) -> dict[str, Path]:
    raw_path = resolve_raw_path(args.input_dir, args.raw)
    if not raw_path.exists():
        raise SystemExit(f"Raw JSONL not found: {raw_path}")
    output_dir = Path(args.output_dir) if args.output_dir else Path("data/replay")
    date_label = args.date or _date_label_from_path(raw_path) or datetime.now().strftime("%Y%m%d")
    csv_path = Path(args.output_csv) if args.output_csv else output_dir / f"opening_auction_attitude_{date_label}.csv"
    md_path = Path(args.output_md) if args.output_md else output_dir / f"opening_auction_attitude_{date_label}.md"
    score_config = build_score_config_from_args(args)
    stock_names = load_stock_names_from_tree(args.name_pool_root) if args.scan_name_pools else {}
    stock_names.update(load_stock_names(args.name_pool))
    rows, summary = replay_raw_jsonl(raw_path, config=score_config, stock_names=stock_names, max_lines=args.max_lines)
    write_csv(rows, csv_path)
    write_markdown(rows, summary, md_path)
    return {"csv": csv_path, "markdown": md_path}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay opening-auction L2 probe data into attitude labels.")
    parser.add_argument("--input-dir", default="", help="Probe output directory containing opening_l2_raw.jsonl.")
    parser.add_argument("--raw", default="", help="Explicit opening_l2_raw.jsonl path.")
    parser.add_argument("--output-dir", default="", help="Output directory. Defaults to data/replay.")
    parser.add_argument("--output-csv", default="", help="Explicit CSV output path.")
    parser.add_argument("--output-md", default="", help="Explicit markdown output path.")
    parser.add_argument("--name-pool", default=DEFAULT_NAME_POOL, help="CSV pool used to map stock codes to names.")
    parser.add_argument("--name-pool-root", default=DEFAULT_NAME_POOL_ROOT, help="Directory scanned for CSV stock-name maps.")
    parser.add_argument("--scan-name-pools", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--min-final-gap-pct", type=float, default=None)
    parser.add_argument("--min-low-to-final-lift-pct", type=float, default=None)
    parser.add_argument("--min-money-lift-ratio", type=float, default=None)
    parser.add_argument("--strong-money-lift-ratio", type=float, default=None)
    parser.add_argument("--close-to-high-tolerance-pct", type=float, default=None)
    parser.add_argument("--big-buy-ratio-threshold", type=float, default=None)
    parser.add_argument("--sell-pressure-ratio-threshold", type=float, default=None)
    parser.add_argument("--date", default="", help="Output date label. Defaults to raw parent prefix or today.")
    parser.add_argument("--max-lines", type=int, default=0, help="Optional debug limit; 0 means all lines.")
    return parser


def build_score_config_from_args(args: argparse.Namespace) -> AuctionScoreConfig:
    defaults = AuctionScoreConfig()
    return AuctionScoreConfig(
        min_low_to_final_lift_pct=_arg_float(
            args.min_low_to_final_lift_pct, defaults.min_low_to_final_lift_pct
        ),
        min_final_gap_pct=_arg_float(args.min_final_gap_pct, defaults.min_final_gap_pct),
        min_money_lift_ratio=_arg_float(args.min_money_lift_ratio, defaults.min_money_lift_ratio),
        strong_money_lift_ratio=_arg_float(args.strong_money_lift_ratio, defaults.strong_money_lift_ratio),
        close_to_high_tolerance_pct=_arg_float(
            args.close_to_high_tolerance_pct, defaults.close_to_high_tolerance_pct
        ),
        big_buy_ratio_threshold=_arg_float(args.big_buy_ratio_threshold, defaults.big_buy_ratio_threshold),
        sell_pressure_ratio_threshold=_arg_float(
            args.sell_pressure_ratio_threshold, defaults.sell_pressure_ratio_threshold
        ),
    )


def main() -> None:
    paths = run_replay(build_parser().parse_args())
    print(f"CSV_PATH {paths['csv']}")
    print(f"MARKDOWN_PATH {paths['markdown']}")


def _trade_side(normalized: dict[str, Any]) -> str:
    side = str(normalized.get("side") or "").strip().upper()
    if side in {"BUY", "B", "1"}:
        return "BUY"
    if side in {"SELL", "S", "2"}:
        return "SELL"
    trade_flag = _to_int(normalized.get("trade_flag") if "trade_flag" in normalized else normalized.get("tradeFlag"))
    if trade_flag == 1:
        return "BUY"
    if trade_flag == 2:
        return "SELL"
    return ""


def _to_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _arg_float(value: float | None, default: float) -> float:
    return float(default if value is None else value)


def _clock_text(value: object) -> str:
    text = str(value or "")
    if len(text) >= 19 and text[10] in {" ", "T"}:
        return text[11:19]
    return text


def _first_positive_float(mapping: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = _to_float(mapping.get(key))
        if value > 0:
            return value
    return 0.0


def _auction_quote_amount(raw_fields: dict[str, Any], *, fallback_price: float, quote_volume_unit: float) -> dict[str, Any]:
    bid_prices = _list_values(raw_fields.get("bidPrice"))
    ask_prices = _list_values(raw_fields.get("askPrice"))
    bid_volumes = _list_values(raw_fields.get("bidVol"))
    ask_volumes = _list_values(raw_fields.get("askVol"))
    bid0 = bid_prices[0] if bid_prices else 0.0
    ask0 = ask_prices[0] if ask_prices else 0.0
    price = bid0 or ask0 or fallback_price
    is_auction_book = bid0 > 0 and ask0 > 0 and abs(bid0 - ask0) < 1e-8
    if bid0 > 0 and ask0 > 0:
        price = bid0 if is_auction_book else fallback_price or bid0 or ask0

    bid_vol0 = bid_volumes[0] if bid_volumes else 0.0
    ask_vol0 = ask_volumes[0] if ask_volumes else 0.0
    if is_auction_book:
        matched_volume = min(bid_vol0, ask_vol0) if bid_vol0 > 0 and ask_vol0 > 0 else max(bid_vol0, ask_vol0)
        unmatched_buy_volume = bid_volumes[1] if len(bid_volumes) > 1 else 0.0
        unmatched_sell_volume = ask_volumes[1] if len(ask_volumes) > 1 else 0.0
    else:
        matched_volume = 0.0
        unmatched_buy_volume = 0.0
        unmatched_sell_volume = 0.0
    if is_auction_book and price > 0 and matched_volume > 0:
        return {
            "price": price,
            "matched_volume": matched_volume,
            "unmatched_buy_volume": unmatched_buy_volume,
            "unmatched_sell_volume": unmatched_sell_volume,
            "matched_amount": price * matched_volume * quote_volume_unit,
            "unmatched_buy_amount": price * unmatched_buy_volume * quote_volume_unit,
            "unmatched_sell_amount": price * unmatched_sell_volume * quote_volume_unit,
            "amount_source": "auction_book",
        }
    amount = _first_positive_float(raw_fields, ("amount", "matchAmount", "matchedAmount", "turnover"))
    raw_volume = _first_positive_float(raw_fields, ("pvolume", "volume"))
    return {
        "price": price,
        "matched_volume": raw_volume,
        "unmatched_buy_volume": unmatched_buy_volume,
        "unmatched_sell_volume": unmatched_sell_volume,
        "matched_amount": amount,
        "unmatched_buy_amount": price * unmatched_buy_volume * quote_volume_unit if price > 0 else 0.0,
        "unmatched_sell_amount": price * unmatched_sell_volume * quote_volume_unit if price > 0 else 0.0,
        "amount_source": "raw_amount" if amount > 0 else "",
    }


def _list_values(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value:
        result.append(_to_float(item))
    return result


def _format_dt(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="milliseconds")
    return str(value or "")


def _date_label_from_path(path: Path) -> str:
    name = path.parent.name
    if len(name) >= 8 and name[:8].isdigit():
        return name[:8]
    return ""


if __name__ == "__main__":
    main()
