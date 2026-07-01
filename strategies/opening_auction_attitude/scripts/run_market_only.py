"""Run OpeningAuctionAttitude in market-only observe-only mode.

This entry point does not connect a trading account and does not place orders.
It subscribes the configured symbols, collects opening-auction tick/Level2
evidence, and logs one MSF_AUCTION_ATTITUDE payload per strategy on exit.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import Settings
from main import _log_runtime_startup_config, _start_runtime_heartbeat, build_app
from monitor.logger import get_log_file_path, get_logger
from strategy.models import StrategyConfig
from strategies.opening_auction_attitude import (
    AUCTION_BIG_ORDER_CONFIRMED,
    AUCTION_BIG_TRADE_CONFIRMED,
    AUCTION_MONEY_LIFT,
    AUCTION_STRONG_CONFIRMED,
    OpeningAuctionAttitudeStrategy,
)
from scripts.pool.common import limit_up_price as calc_limit_up_price


DEFAULT_POOL = "data/stock_pools/current/opening_auction_universe.csv"
SESSION_EVENT_PREFIX = "OPENING_AUCTION_ATTITUDE_SESSION"
EVENT_NAME = "MSF_AUCTION_ATTITUDE"
RANKING_EVENT_NAME = "MSF_AUCTION_RANKING"
BUY_PLAN_EVENT_NAME = "MSF_AUCTION_BUY_PLAN"
MATCHED_CANDIDATES_EVENT_NAME = "MSF_AUCTION_MATCHED_CANDIDATES"

ACTIONABLE_AUCTION_LABELS = {
    AUCTION_STRONG_CONFIRMED,
    AUCTION_BIG_ORDER_CONFIRMED,
    AUCTION_BIG_TRADE_CONFIRMED,
    AUCTION_MONEY_LIFT,
}
AUCTION_LABEL_BONUS = {
    AUCTION_STRONG_CONFIRMED: 20.0,
    AUCTION_BIG_ORDER_CONFIRMED: 14.0,
    AUCTION_BIG_TRADE_CONFIRMED: 14.0,
    AUCTION_MONEY_LIFT: 8.0,
}
DEFAULT_BUY_PLAN_TOP_N = 0
DEFAULT_BUY_PLAN_MIN_SCORE = 75.0
DEFAULT_BUY_PLAN_AMOUNT = 0.0
MIN_FILTER_FINAL_AUCTION_AMOUNT = 30_000_000.0
MIN_FILTER_OPEN_PCT = 3.0
RANKING_HEADERS = [
    "rank",
    "stock_code",
    "stock_name",
    "auction_label",
    "auction_rank_score",
    "auction_attitude_score",
    "auction_speed_score",
    "final_gap_pct",
    "low_to_final_lift_pct",
    "low_to_final_amount_ratio",
    "amount_at_final",
    "open_pct",
    "open_price_0925",
    "post_0920_low_price",
    "post_0920_low_time",
    "final_vs_post_0920_low_pct",
    "final_auction_amount",
    "filter_final_amount_gt_3000w",
    "filter_final_price_gt_post_0920_low",
    "filter_open_pct_gt_3",
    "final_amount_source",
    "tx_detail_available",
    "final_tx_amount",
    "final_tx_count",
    "last10_bid_amount",
    "last20_bid_amount",
    "final_from_last20_bid_amount",
    "final_from_last20_bid_pct",
    "final_from_last10_bid_amount",
    "final_from_last10_bid_pct",
    "limit_up_price",
    "final_from_limit_up_bid_amount",
    "final_from_limit_up_bid_pct",
    "big_order_buy_ratio",
    "big_trade_buy_ratio",
    "has_order_confirmation",
    "has_trade_confirmation",
    "has_sell_pressure",
    "plan_eligible",
    "reason",
]
BUY_PLAN_HEADERS = [
    "rank",
    "stock_code",
    "stock_name",
    "plan_amount",
    "reference_price",
    "post_0920_low_price",
    "post_0920_low_time",
    "open_pct",
    "final_auction_amount",
    "last10_bid_amount",
    "last20_bid_amount",
    "final_from_last20_bid_pct",
    "final_from_last10_bid_pct",
    "final_from_limit_up_bid_pct",
    "auction_rank_score",
    "auction_label",
    "reason",
    "status",
    "observe_only",
    "real_order_sent",
]
MATCHED_CANDIDATE_HEADERS = [
    "\u6392\u540d",
    "\u80a1\u7968\u4ee3\u7801",
    "\u540d\u79f0",
    "\u7ade\u4ef7\u6da8\u5e45%",
    "\u6700\u7ec8\u7ade\u4ef7\u6210\u4ea4\u989d(\u4e07)",
    "9:20\u540e\u4f4e\u70b9",
    "\u4f4e\u70b9\u65f6\u95f4",
    "\u6700\u7ec8\u8f83\u4f4e\u70b9\u6da8\u5e45%",
    "\u5c3e10\u79d2\u7ade\u4e70\u989d(\u4e07)",
    "\u5c3e20\u79d2\u7ade\u4e70\u989d(\u4e07)",
    "\u5c3e20\u79d2\u4e70\u5355\u6210\u4ea4\u5360\u6bd4%",
    "\u5c3e10\u79d2\u4e70\u5355\u6210\u4ea4\u5360\u6bd4%",
    "\u6da8\u505c\u4ef7\u4e70\u5165\u5360\u6bd4%",
]

CODE_COLUMN_CANDIDATES = (
    "stock_code",
    "code",
    "symbol",
    "\u80a1\u7968\u4ee3\u7801",
    "\u4ee3\u7801",
    "\u8bc1\u5238\u4ee3\u7801",
)
NAME_COLUMN_CANDIDATES = (
    "stock_name",
    "name",
    "\u80a1\u7968\u540d\u79f0",
    "\u540d\u79f0",
    "\u8bc1\u5238\u540d\u79f0",
)


@dataclass(frozen=True)
class PoolEntry:
    stock_code: str
    stock_name: str = ""


@dataclass(frozen=True)
class SnapshotTick:
    stock_code: str
    last_price: float = 0.0
    pre_close: float = 0.0
    amount: float = 0.0
    volume: float = 0.0
    snapshot_time: datetime | None = None
    raw: dict[str, Any] | None = None


def _apply_runtime_settings(runtime_settings) -> None:
    from config.settings import settings as global_runtime_settings

    for name in dir(runtime_settings):
        if not name.isupper():
            continue
        try:
            setattr(global_runtime_settings, name, getattr(runtime_settings, name))
        except Exception:
            continue


def normalize_stock_code(value: object) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    if text.isdigit():
        return text.zfill(6)
    return ""


def parse_codes(values: Iterable[str] | str | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_values = [values]
    else:
        raw_values = list(values)

    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for token in re.split(r"[\s,;，；]+", str(raw or "")):
            code = normalize_stock_code(token)
            if code and code not in seen:
                seen.add(code)
                result.append(code)
    return result


def parse_hhmmss(value: str) -> tuple[int, int, int]:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError(f"invalid time format: {value!r}, expected HH:MM or HH:MM:SS")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid time format: {value!r}, expected HH:MM or HH:MM:SS") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise argparse.ArgumentTypeError(f"invalid time range: {value!r}")
    return hour, minute, second


def build_session_time(anchor: datetime, value: str) -> datetime:
    hour, minute, second = parse_hhmmss(value)
    return anchor.replace(hour=hour, minute=minute, second=second, microsecond=0)


def to_xt_code(stock_code: str) -> str:
    code = normalize_stock_code(stock_code)
    if not code:
        return ""
    if code.startswith(("5", "6", "9", "11")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _extract_scalar(payload: Any, default: Any = None) -> Any:
    if isinstance(payload, (list, tuple)):
        return payload[0] if payload else default
    return payload if payload is not None else default


def _to_float(value: Any) -> float:
    try:
        return float(_extract_scalar(value, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _first_book_price(payload: dict[str, Any]) -> float:
    bid = _to_float(payload.get("bidPrice"))
    ask = _to_float(payload.get("askPrice"))
    if bid > 0 and ask > 0 and abs(bid - ask) < 1e-8:
        return bid
    return bid or ask


def _coerce_snapshot_time(value: Any, fallback: datetime) -> datetime:
    raw = _extract_scalar(value)
    if raw is None:
        return fallback
    try:
        numeric = float(raw)
    except (TypeError, ValueError):
        return fallback
    if numeric <= 0:
        return fallback
    if numeric > 10_000_000_000:
        numeric = numeric / 1000.0
    try:
        return datetime.fromtimestamp(numeric)
    except (OSError, OverflowError, ValueError):
        return fallback


def parse_snapshot_tick(stock_code: str, payload: dict[str, Any], *, recv_time: datetime | None = None) -> SnapshotTick:
    recv_time = recv_time or datetime.now()
    pre_close = (
        _to_float(payload.get("lastClose"))
        or _to_float(payload.get("preClose"))
        or _to_float(payload.get("pre_close"))
    )
    return SnapshotTick(
        stock_code=normalize_stock_code(stock_code),
        last_price=_to_float(payload.get("lastPrice") or payload.get("last_price")) or _first_book_price(payload),
        pre_close=pre_close,
        amount=_to_float(payload.get("amount") or payload.get("turnover")),
        volume=_to_float(payload.get("volume") or payload.get("pvolume")),
        snapshot_time=_coerce_snapshot_time(payload.get("time") or payload.get("sysTime"), recv_time),
        raw=dict(payload),
    )


def fetch_full_tick_snapshots(codes: Iterable[str]) -> dict[str, SnapshotTick]:
    normalized_codes = [code for code in (normalize_stock_code(value) for value in codes) if code]
    if not normalized_codes:
        return {}
    try:
        from xtquant import xtdata
    except ImportError as exc:
        raise RuntimeError("xtquant.xtdata is required for opening auction snapshot polling") from exc

    snapshots: dict[str, SnapshotTick] = {}
    xt_to_code = {to_xt_code(code): code for code in normalized_codes}
    xt_codes = [xt_code for xt_code in xt_to_code if xt_code]
    recv_time = datetime.now()
    for offset in range(0, len(xt_codes), 400):
        batch = xt_codes[offset : offset + 400]
        tick_map = xtdata.get_full_tick(batch) or {}
        for key, payload in tick_map.items():
            code = normalize_stock_code(key) or xt_to_code.get(str(key), "")
            if not code or not isinstance(payload, dict):
                continue
            snapshots[code] = parse_snapshot_tick(code, payload, recv_time=recv_time)
    return snapshots


def _find_column(columns: Iterable[str], candidates: tuple[str, ...]) -> str:
    normalized = {str(column or "").strip().lower(): str(column or "") for column in columns}
    for candidate in candidates:
        found = normalized.get(candidate.lower())
        if found:
            return found
    return ""


def _read_pool_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "gbk"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                fieldnames = list(reader.fieldnames or [])
                return fieldnames, [dict(row) for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return [], []


def load_pool_entries(pool_path: str) -> list[PoolEntry]:
    if not pool_path:
        return []
    path = Path(pool_path)
    if not path.is_file():
        return []

    fieldnames, rows = _read_pool_rows(path)
    if not fieldnames:
        return []

    code_column = _find_column(fieldnames, CODE_COLUMN_CANDIDATES)
    name_column = _find_column(fieldnames, NAME_COLUMN_CANDIDATES)
    if not code_column:
        code_column = fieldnames[0]

    entries: list[PoolEntry] = []
    seen: set[str] = set()
    for row in rows:
        code = normalize_stock_code(row.get(code_column))
        if not code or code in seen:
            continue
        seen.add(code)
        name = str(row.get(name_column) or "").strip() if name_column else ""
        entries.append(PoolEntry(stock_code=code, stock_name=name))
    return entries


def resolve_observe_entries(
    *,
    codes: Iterable[str] | str | None = None,
    pool_path: str = DEFAULT_POOL,
    max_count: int = 0,
) -> list[PoolEntry]:
    pool_entries = load_pool_entries(pool_path)
    names_by_code = {entry.stock_code: entry.stock_name for entry in pool_entries}
    requested_codes = parse_codes(codes)

    if requested_codes:
        entries = [PoolEntry(stock_code=code, stock_name=names_by_code.get(code, "")) for code in requested_codes]
    else:
        entries = pool_entries

    if max_count and max_count > 0:
        entries = entries[: int(max_count)]
    return entries


def build_strategy_configs(entries: Iterable[PoolEntry]) -> list[StrategyConfig]:
    configs: list[StrategyConfig] = []
    for entry in entries:
        code = normalize_stock_code(entry.stock_code)
        if not code:
            continue
        configs.append(
            StrategyConfig(
                stock_code=code,
                max_position_amount=0.0,
                params={
                    "instance_key": code,
                    "stock_name": str(entry.stock_name or ""),
                    "observe_only": True,
                    "quote_volume_unit": 100,
                },
            )
        )
    return configs


def install_observe_strategies(
    runner,
    configs: Iterable[StrategyConfig],
    *,
    trade_executor=None,
    position_manager=None,
) -> int:
    installed = 0
    for config in configs:
        runner.add_strategy(OpeningAuctionAttitudeStrategy(config, trade_executor, position_manager))
        installed += 1
    return installed


class OpeningAuctionLimitUpScanner:
    def __init__(
        self,
        entries: Iterable[PoolEntry],
        *,
        snapshot_provider: Callable[[Iterable[str]], dict[str, SnapshotTick]],
        freeze_at: datetime,
        limit_up_tolerance: float = 0.01,
        snapshot_record_path: str = "",
        logger=None,
    ):
        self._entries = [
            PoolEntry(normalize_stock_code(entry.stock_code), entry.stock_name)
            for entry in entries
            if normalize_stock_code(entry.stock_code)
        ]
        self._entries_by_code = {entry.stock_code: entry for entry in self._entries}
        self._snapshot_provider = snapshot_provider
        self._freeze_at = freeze_at
        self._limit_up_tolerance = max(0.0, float(limit_up_tolerance or 0.0))
        self._logger = logger or get_logger("system")
        self._candidate_codes: set[str] = set()
        self._frozen = False
        self._snapshot_record_handle = None
        if snapshot_record_path:
            path = Path(snapshot_record_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._snapshot_record_handle = path.open("a", encoding="utf-8", newline="")

    @property
    def candidate_count(self) -> int:
        return len(self._candidate_codes)

    @property
    def universe_count(self) -> int:
        return len(self._entries)

    def close(self) -> None:
        if self._snapshot_record_handle is not None:
            self._snapshot_record_handle.close()
            self._snapshot_record_handle = None

    def scan_once(self, now: datetime) -> list[PoolEntry]:
        if self._frozen or now >= self._freeze_at:
            self._frozen = True
            return []

        pending_codes = [entry.stock_code for entry in self._entries if entry.stock_code not in self._candidate_codes]
        if not pending_codes:
            return []

        snapshots = self._snapshot_provider(pending_codes)
        found: list[PoolEntry] = []
        for code, tick in snapshots.items():
            normalized_code = normalize_stock_code(code)
            if not normalized_code or normalized_code in self._candidate_codes:
                continue
            entry = self._entries_by_code.get(normalized_code)
            if not entry:
                continue
            last_price = float(tick.last_price or 0.0)
            pre_close = float(tick.pre_close or 0.0)
            limit_price = calc_limit_up_price(pre_close)
            is_hit = limit_price > 0 and last_price > 0 and last_price >= limit_price - self._limit_up_tolerance
            self._record_snapshot(now, entry, tick, limit_price=limit_price, is_hit=is_hit)
            if limit_price <= 0 or last_price <= 0:
                continue
            if not is_hit:
                continue
            self._candidate_codes.add(normalized_code)
            found.append(entry)
            self._logger.info(
                "%s candidate_found stock=%s name=%s last_price=%.3f pre_close=%.3f limit_up=%.3f amount=%.0f snapshot_time=%s",
                SESSION_EVENT_PREFIX,
                normalized_code,
                entry.stock_name,
                last_price,
                pre_close,
                limit_price,
                float(tick.amount or 0.0),
                tick.snapshot_time.strftime("%H:%M:%S") if tick.snapshot_time else "",
            )
        return found

    def _record_snapshot(
        self,
        now: datetime,
        entry: PoolEntry,
        tick: SnapshotTick,
        *,
        limit_price: float,
        is_hit: bool,
    ) -> None:
        if self._snapshot_record_handle is None:
            return
        row = build_snapshot_record_row(
            now,
            entry,
            tick,
            limit_price=limit_price,
            is_hit=is_hit,
            record_mode="dynamic_candidates",
        )
        self._snapshot_record_handle.write(
            json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n"
        )
        self._snapshot_record_handle.flush()


class FullPoolSnapshotRecorder:
    """Periodically record full-tick snapshots for every loaded candidate.

    This is the default install-all companion to the legacy dynamic candidate
    scanner. It does not select candidates; it only preserves market snapshots
    for replay/debugging alongside the separate L2 raw recorder.
    """

    def __init__(
        self,
        entries: Iterable[PoolEntry],
        *,
        snapshot_provider: Callable[[Iterable[str]], dict[str, SnapshotTick]],
        snapshot_record_path: str = "",
        limit_up_tolerance: float = 0.01,
    ):
        self._entries = [
            PoolEntry(normalize_stock_code(entry.stock_code), entry.stock_name)
            for entry in entries
            if normalize_stock_code(entry.stock_code)
        ]
        self._entries_by_code = {entry.stock_code: entry for entry in self._entries}
        self._snapshot_provider = snapshot_provider
        self._limit_up_tolerance = max(0.0, float(limit_up_tolerance or 0.0))
        self._rows_written = 0
        self._snapshot_record_handle = None
        if snapshot_record_path:
            path = Path(snapshot_record_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._snapshot_record_handle = path.open("a", encoding="utf-8", newline="")

    @property
    def universe_count(self) -> int:
        return len(self._entries)

    @property
    def rows_written(self) -> int:
        return self._rows_written

    def close(self) -> None:
        if self._snapshot_record_handle is not None:
            self._snapshot_record_handle.close()
            self._snapshot_record_handle = None

    def record_once(self, now: datetime) -> int:
        if self._snapshot_record_handle is None or not self._entries:
            return 0
        snapshots = self._snapshot_provider([entry.stock_code for entry in self._entries])
        written = 0
        for code, tick in snapshots.items():
            normalized_code = normalize_stock_code(code)
            entry = self._entries_by_code.get(normalized_code)
            if not entry:
                continue
            last_price = float(tick.last_price or 0.0)
            pre_close = float(tick.pre_close or 0.0)
            limit_price = calc_limit_up_price(pre_close)
            is_hit = limit_price > 0 and last_price > 0 and last_price >= limit_price - self._limit_up_tolerance
            row = build_snapshot_record_row(
                now,
                entry,
                tick,
                limit_price=limit_price,
                is_hit=is_hit,
                record_mode="install_all_full_pool",
            )
            self._snapshot_record_handle.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n"
            )
            written += 1
        if written:
            self._snapshot_record_handle.flush()
            self._rows_written += written
        return written


def build_snapshot_record_row(
    now: datetime,
    entry: PoolEntry,
    tick: SnapshotTick,
    *,
    limit_price: float,
    is_hit: bool,
    record_mode: str,
) -> dict[str, Any]:
    return {
        "record_mode": record_mode,
        "scan_time": now,
        "stock": entry.stock_code,
        "stock_name": entry.stock_name,
        "last_price": float(tick.last_price or 0.0),
        "pre_close": float(tick.pre_close or 0.0),
        "limit_up_price": float(limit_price or 0.0),
        "amount": float(tick.amount or 0.0),
        "volume": float(tick.volume or 0.0),
        "snapshot_time": tick.snapshot_time,
        "is_hit": bool(is_hit),
        "raw": tick.raw or {},
    }


def build_observe_settings(args: argparse.Namespace) -> Settings:
    overrides = {
        "CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN": True,
        "LOAD_PREVIOUS_STATE_ON_START": False,
        "LOG_SUMMARY_MODE": not bool(args.full_console),
        "SESSION_EXIT_TIME": str(args.stop_time),
    }
    if int(args.heartbeat_interval_sec) > 0:
        overrides["RUNTIME_HEARTBEAT_INTERVAL_SEC"] = int(args.heartbeat_interval_sec)
    return Settings(**overrides)


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def default_output_path(kind: str, anchor: datetime | None = None) -> str:
    date_label = (anchor or datetime.now()).strftime("%Y%m%d")
    filename = f"{date_label}_{kind}.csv"
    return str(Path(__file__).resolve().parents[1] / "output" / filename)


def _to_output_float(value: Any, digits: int = 6) -> float:
    try:
        return round(float(value or 0.0), digits)
    except (TypeError, ValueError):
        return 0.0


def _auction_rank_score(decision) -> float:
    label = str(getattr(decision, "auction_label", "") or "")
    evidence = dict(getattr(decision, "evidence", {}) or {})
    score = (
        float(getattr(decision, "auction_attitude_score", 0.0) or 0.0) * 0.75
        + float(getattr(decision, "auction_speed_score", 0.0) or 0.0) * 0.25
        + float(AUCTION_LABEL_BONUS.get(label, 0.0))
    )
    if evidence.get("has_order_sell_pressure") or evidence.get("has_trade_sell_pressure"):
        score -= 15.0
    if evidence.get("has_unmatched_sell_pressure"):
        score -= 8.0
    return round(max(0.0, min(120.0, score)), 3)


def _auction_reference_metrics(strategy: OpeningAuctionAttitudeStrategy) -> dict[str, Any]:
    try:
        return dict(strategy.build_auction_reference_metrics())
    except Exception:
        return {}


def _condition_filter(reference: dict[str, Any]) -> tuple[bool, str]:
    final_amount = float(reference.get("final_auction_amount", 0.0) or 0.0)
    final_price = float(reference.get("open_price_0925", 0.0) or 0.0)
    post_low = float(reference.get("post_0920_low_price", 0.0) or 0.0)
    open_pct = float(reference.get("open_pct", 0.0) or 0.0)
    checks = {
        "final_amount_gt_3000w": final_amount > MIN_FILTER_FINAL_AUCTION_AMOUNT,
        "final_price_gt_post_0920_low": post_low > 0 and final_price > post_low,
        "open_pct_gt_3": open_pct > MIN_FILTER_OPEN_PCT,
    }
    if all(checks.values()):
        return True, "condition_filter_matched"
    failed = [name for name, passed in checks.items() if not passed]
    return False, "condition_filter_failed:" + ",".join(failed)


def build_auction_rankings(
    strategies: Iterable[OpeningAuctionAttitudeStrategy],
    *,
    min_plan_score: float = DEFAULT_BUY_PLAN_MIN_SCORE,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for strategy in strategies:
        if not isinstance(strategy, OpeningAuctionAttitudeStrategy):
            continue
        decision = strategy.classify_auction()
        evidence = dict(decision.evidence or {})
        reference = _auction_reference_metrics(strategy)
        params = getattr(getattr(strategy, "config", None), "params", {}) or {}
        label = str(decision.auction_label or "")
        sell_pressure = bool(
            evidence.get("has_order_sell_pressure")
            or evidence.get("has_trade_sell_pressure")
            or evidence.get("has_unmatched_sell_pressure")
        )
        plan_eligible, filter_reason = _condition_filter(reference)
        rows.append(
            {
                "rank": 0,
                "stock_code": strategy.stock_code,
                "stock_name": str(params.get("stock_name") or ""),
                "auction_label": label,
                "auction_rank_score": 0.0,
                "auction_attitude_score": 0.0,
                "auction_speed_score": 0.0,
                "final_gap_pct": _to_output_float(evidence.get("final_gap_pct"), 6),
                "low_to_final_lift_pct": _to_output_float(evidence.get("low_to_final_lift_pct"), 6),
                "low_to_final_amount_ratio": _to_output_float(evidence.get("low_to_final_amount_ratio"), 6),
                "amount_at_final": _to_output_float(evidence.get("amount_at_final"), 2),
                "open_pct": _to_output_float(reference.get("open_pct"), 2),
                "open_price_0925": _to_output_float(reference.get("open_price_0925"), 3),
                "post_0920_low_price": _to_output_float(reference.get("post_0920_low_price"), 3),
                "post_0920_low_time": str(reference.get("post_0920_low_time") or ""),
                "final_vs_post_0920_low_pct": _to_output_float(reference.get("final_vs_post_0920_low_pct"), 2),
                "final_auction_amount": _to_output_float(reference.get("final_auction_amount"), 2),
                "filter_final_amount_gt_3000w": bool(reference.get("final_amount_gt_3000w")),
                "filter_final_price_gt_post_0920_low": bool(reference.get("final_price_gt_post_0920_low")),
                "filter_open_pct_gt_3": bool(reference.get("open_pct_gt_3")),
                "final_amount_source": str(reference.get("final_amount_source") or ""),
                "tx_detail_available": bool(reference.get("tx_detail_available")),
                "final_tx_amount": _to_output_float(reference.get("final_tx_amount"), 2),
                "final_tx_count": int(reference.get("final_tx_count", 0) or 0),
                "last10_bid_amount": _to_output_float(reference.get("last10_bid_amount"), 2),
                "last20_bid_amount": _to_output_float(reference.get("last20_bid_amount"), 2),
                "final_from_last20_bid_amount": _to_output_float(reference.get("final_from_last20_bid_amount"), 2),
                "final_from_last20_bid_pct": _to_output_float(reference.get("final_from_last20_bid_pct"), 2),
                "final_from_last10_bid_amount": _to_output_float(reference.get("final_from_last10_bid_amount"), 2),
                "final_from_last10_bid_pct": _to_output_float(reference.get("final_from_last10_bid_pct"), 2),
                "limit_up_price": _to_output_float(reference.get("limit_up_price"), 3),
                "final_from_limit_up_bid_amount": _to_output_float(reference.get("final_from_limit_up_bid_amount"), 2),
                "final_from_limit_up_bid_pct": _to_output_float(reference.get("final_from_limit_up_bid_pct"), 2),
                "big_order_buy_ratio": _to_output_float(evidence.get("big_order_buy_ratio"), 6),
                "big_trade_buy_ratio": _to_output_float(evidence.get("big_trade_buy_ratio"), 6),
                "has_order_confirmation": bool(evidence.get("has_order_confirmation")),
                "has_trade_confirmation": bool(evidence.get("has_trade_confirmation")),
                "has_sell_pressure": sell_pressure,
                "plan_eligible": plan_eligible,
                "reason": filter_reason,
                "reference_price": _to_output_float(reference.get("open_price_0925"), 3),
            }
        )

    rows.sort(
        key=lambda row: (
            not bool(row["plan_eligible"]),
            -float(row["final_auction_amount"]),
            -float(row["open_pct"]),
            -float(row["final_from_last20_bid_pct"]),
            str(row["stock_code"]),
        )
    )
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows


def build_buy_plan_rows(
    rankings: Iterable[dict[str, Any]],
    *,
    top_n: int = DEFAULT_BUY_PLAN_TOP_N,
    plan_amount: float = DEFAULT_BUY_PLAN_AMOUNT,
) -> list[dict[str, Any]]:
    limit = int(top_n or 0)
    rows: list[dict[str, Any]] = []
    for row in rankings:
        if not bool(row.get("plan_eligible")):
            continue
        rows.append(
            {
                "rank": int(row.get("rank", 0) or 0),
                "stock_code": str(row.get("stock_code") or ""),
                "stock_name": str(row.get("stock_name") or ""),
                "plan_amount": _to_output_float(plan_amount, 2),
                "reference_price": _to_output_float(row.get("reference_price"), 3),
                "post_0920_low_price": _to_output_float(row.get("post_0920_low_price"), 3),
                "post_0920_low_time": str(row.get("post_0920_low_time") or ""),
                "open_pct": _to_output_float(row.get("open_pct"), 2),
                "final_auction_amount": _to_output_float(row.get("final_auction_amount"), 2),
                "last10_bid_amount": _to_output_float(row.get("last10_bid_amount"), 2),
                "last20_bid_amount": _to_output_float(row.get("last20_bid_amount"), 2),
                "final_from_last20_bid_pct": _to_output_float(row.get("final_from_last20_bid_pct"), 2),
                "final_from_last10_bid_pct": _to_output_float(row.get("final_from_last10_bid_pct"), 2),
                "final_from_limit_up_bid_pct": _to_output_float(row.get("final_from_limit_up_bid_pct"), 2),
                "auction_rank_score": _to_output_float(row.get("auction_rank_score"), 3),
                "auction_label": str(row.get("auction_label") or ""),
                "reason": str(row.get("reason") or ""),
                "status": "PLAN_ONLY",
                "observe_only": True,
                "real_order_sent": False,
            }
        )
        if limit > 0 and len(rows) >= limit:
            break
    return rows


def _to_wan(value: Any, digits: int = 1) -> float:
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    return _to_output_float(amount / 10_000.0, digits)


def build_matched_candidate_rows(rankings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in rankings:
        if not bool(source.get("plan_eligible")):
            continue
        rows.append(
            {
                "\u6392\u540d": len(rows) + 1,
                "\u80a1\u7968\u4ee3\u7801": str(source.get("stock_code") or ""),
                "\u540d\u79f0": str(source.get("stock_name") or ""),
                "\u7ade\u4ef7\u6da8\u5e45%": _to_output_float(source.get("open_pct"), 2),
                "\u6700\u7ec8\u7ade\u4ef7\u6210\u4ea4\u989d(\u4e07)": _to_wan(source.get("final_auction_amount"), 1),
                "9:20\u540e\u4f4e\u70b9": _to_output_float(source.get("post_0920_low_price"), 3),
                "\u4f4e\u70b9\u65f6\u95f4": str(source.get("post_0920_low_time") or ""),
                "\u6700\u7ec8\u8f83\u4f4e\u70b9\u6da8\u5e45%": _to_output_float(source.get("final_vs_post_0920_low_pct"), 2),
                "\u5c3e10\u79d2\u7ade\u4e70\u989d(\u4e07)": _to_wan(source.get("last10_bid_amount"), 1),
                "\u5c3e20\u79d2\u7ade\u4e70\u989d(\u4e07)": _to_wan(source.get("last20_bid_amount"), 1),
                "\u5c3e20\u79d2\u4e70\u5355\u6210\u4ea4\u5360\u6bd4%": _to_output_float(source.get("final_from_last20_bid_pct"), 2),
                "\u5c3e10\u79d2\u4e70\u5355\u6210\u4ea4\u5360\u6bd4%": _to_output_float(source.get("final_from_last10_bid_pct"), 2),
                "\u6da8\u505c\u4ef7\u4e70\u5165\u5360\u6bd4%": _to_output_float(source.get("final_from_limit_up_bid_pct"), 2),
            }
        )
    return rows


def render_matched_candidates_markdown(rows: Iterable[dict[str, Any]]) -> str:
    table_rows = list(rows)
    lines = [
        "# Opening Auction Matched Candidates",
        "",
        "| " + " | ".join(MATCHED_CANDIDATE_HEADERS) + " |",
        "| " + " | ".join(["---"] * len(MATCHED_CANDIDATE_HEADERS)) + " |",
    ]
    for row in table_rows:
        lines.append("| " + " | ".join(str(row.get(header, "")) for header in MATCHED_CANDIDATE_HEADERS) + " |")
    return "\n".join(lines) + "\n"


def _write_rows(path: str, rows: Iterable[dict[str, Any]], headers: list[str]) -> str:
    if not path:
        return ""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return str(output_path)


def write_auction_rankings(path: str, rankings: Iterable[dict[str, Any]]) -> str:
    return _write_rows(path, rankings, RANKING_HEADERS)


def write_buy_plan(path: str, buy_plan_rows: Iterable[dict[str, Any]]) -> str:
    return _write_rows(path, buy_plan_rows, BUY_PLAN_HEADERS)


def write_matched_candidates(path: str, rows: Iterable[dict[str, Any]]) -> str:
    return _write_rows(path, rows, MATCHED_CANDIDATE_HEADERS)


def write_matched_candidates_markdown(path: str, rows: Iterable[dict[str, Any]]) -> str:
    if not path:
        return ""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_matched_candidates_markdown(rows), encoding="utf-8")
    return str(output_path)


def emit_auction_rankings_and_buy_plan(
    runner,
    *,
    logger=None,
    ranking_output_path: str = "",
    buy_plan_output_path: str = "",
    matched_candidates_output_path: str = "",
    matched_candidates_md_output_path: str = "",
    buy_plan_top_n: int = DEFAULT_BUY_PLAN_TOP_N,
    buy_plan_min_score: float = DEFAULT_BUY_PLAN_MIN_SCORE,
    buy_plan_amount: float = DEFAULT_BUY_PLAN_AMOUNT,
) -> tuple[int, int]:
    logger = logger or get_logger("system")
    rankings = build_auction_rankings(runner.get_all_strategies(), min_plan_score=buy_plan_min_score)
    buy_plan_rows = build_buy_plan_rows(rankings, top_n=buy_plan_top_n, plan_amount=buy_plan_amount)
    matched_candidate_rows = build_matched_candidate_rows(rankings)
    ranking_path = write_auction_rankings(ranking_output_path, rankings)
    plan_path = write_buy_plan(buy_plan_output_path, buy_plan_rows)
    matched_path = write_matched_candidates(matched_candidates_output_path, matched_candidate_rows)
    matched_md_path = write_matched_candidates_markdown(matched_candidates_md_output_path, matched_candidate_rows)
    logger.info(
        "%s %s",
        RANKING_EVENT_NAME,
        json.dumps(
            {
                "event_name": RANKING_EVENT_NAME,
                "rows": len(rankings),
                "output_path": ranking_path,
                "observe_only": True,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        ),
    )
    logger.info(
        "%s %s",
        BUY_PLAN_EVENT_NAME,
        json.dumps(
            {
                "event_name": BUY_PLAN_EVENT_NAME,
                "rows": len(buy_plan_rows),
                "top_n": int(buy_plan_top_n or 0),
                "min_score": float(buy_plan_min_score or 0.0),
                "plan_amount": float(buy_plan_amount or 0.0),
                "output_path": plan_path,
                "observe_only": True,
                "real_order_sent": False,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        ),
    )
    logger.info(
        "%s %s",
        MATCHED_CANDIDATES_EVENT_NAME,
        json.dumps(
            {
                "event_name": MATCHED_CANDIDATES_EVENT_NAME,
                "rows": len(matched_candidate_rows),
                "output_path": matched_path,
                "markdown_output_path": matched_md_path,
                "observe_only": True,
                "real_order_sent": False,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        ),
    )
    return len(rankings), len(buy_plan_rows)


def emit_auction_decisions(runner, logger=None) -> int:
    logger = logger or get_logger("system")
    emitted = 0
    for strategy in runner.get_all_strategies():
        if not isinstance(strategy, OpeningAuctionAttitudeStrategy):
            continue
        decision = strategy.classify_auction()
        payload = strategy.build_event_payload(decision)
        params = getattr(getattr(strategy, "config", None), "params", {}) or {}
        payload["stock_name"] = str(params.get("stock_name") or "")
        payload["observe_only"] = True
        logger.info(
            "%s %s",
            EVENT_NAME,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default),
        )
        emitted += 1
    return emitted


def run_market_only(
    *,
    codes: Iterable[str] | str | None = None,
    pool_path: str = DEFAULT_POOL,
    max_count: int = 0,
    settings=None,
    stop_at: Optional[datetime] = None,
    mode: str = "opening-auction-attitude-market-only",
    session_event_prefix: str = SESSION_EVENT_PREFIX,
    stop_reason: str = "scheduled_stop",
    now_provider: Callable[[], datetime] = datetime.now,
    sleep_fn: Callable[[float], None] = time.sleep,
    build_app_fn=build_app,
    dynamic_candidates: bool = False,
    snapshot_provider: Callable[[Iterable[str]], dict[str, SnapshotTick]] = fetch_full_tick_snapshots,
    scan_start_time: str = "09:15:00",
    candidate_freeze_time: str = "09:24:30",
    snapshot_interval_sec: float = 2.0,
    limit_up_tolerance: float = 0.01,
    snapshot_record_path: str = "",
    ranking_output_path: str = "",
    buy_plan_output_path: str = "",
    matched_candidates_output_path: str = "",
    matched_candidates_md_output_path: str = "",
    preopen_reference_time: str = "09:25:15",
    buy_plan_top_n: int = DEFAULT_BUY_PLAN_TOP_N,
    buy_plan_min_score: float = DEFAULT_BUY_PLAN_MIN_SCORE,
    buy_plan_amount: float = DEFAULT_BUY_PLAN_AMOUNT,
) -> None:
    entries = resolve_observe_entries(codes=codes, pool_path=pool_path, max_count=max_count)
    if not entries:
        raise SystemExit("No opening-auction observe symbols. Use --codes or a valid --pool file.")
    configs = build_strategy_configs(entries)

    if settings is None:
        settings = Settings(
            CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN=True,
            LOAD_PREVIOUS_STATE_ON_START=False,
        )
    else:
        settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN = True
        settings.LOAD_PREVIOUS_STATE_ON_START = False

    _apply_runtime_settings(settings)

    ctx = build_app_fn(strategy_classes=[], settings=settings)
    logger = get_logger("system")
    settings = ctx["settings"]
    conn_mgr = ctx["conn_mgr"]
    runner = ctx["runner"]
    data_sub = ctx["data_sub"]
    trade_exec = ctx.get("trade_exec")
    pos_mgr = ctx.get("pos_mgr")
    stop_event = threading.Event()
    anchor = now_provider()
    scan_start_at = build_session_time(anchor, scan_start_time)
    freeze_at = build_session_time(anchor, candidate_freeze_time)
    preopen_reference_at = build_session_time(anchor, preopen_reference_time)
    snapshot_interval_sec = max(0.2, float(snapshot_interval_sec or 0.0))
    scanner = (
        OpeningAuctionLimitUpScanner(
            entries,
            snapshot_provider=snapshot_provider,
            freeze_at=freeze_at,
            limit_up_tolerance=limit_up_tolerance,
            snapshot_record_path=snapshot_record_path,
            logger=logger,
        )
        if dynamic_candidates
        else None
    )
    full_snapshot_recorder = (
        FullPoolSnapshotRecorder(
            entries,
            snapshot_provider=snapshot_provider,
            snapshot_record_path=snapshot_record_path,
            limit_up_tolerance=limit_up_tolerance,
        )
        if (not dynamic_candidates and snapshot_record_path)
        else None
    )
    last_full_snapshot_at: datetime | None = None

    def _stop(sig=None, frame=None) -> None:
        logger.info("OpeningAuctionAttitude observe-only session stopping sig=%s", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    logger.info(
        "%s session_start mode=%s dry_run=%s pool=%s codes=%s max_count=%d stop_at=%s account_connected=false",
        session_event_prefix,
        mode,
        settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN,
        pool_path,
        ",".join(entry.stock_code for entry in entries),
        max_count,
        stop_at.strftime("%H:%M:%S") if stop_at else "",
    )
    _log_runtime_startup_config(settings, conn_mgr, mode=mode)

    started = False
    preopen_reference_emitted = False
    try:
        runner.start()
        installed = 0
        if scanner:
            logger.info(
                "%s scanner_start universe=%d scan_start=%s freeze_time=%s interval_sec=%.1f limit_up_tolerance=%.3f",
                session_event_prefix,
                len(entries),
                scan_start_at.strftime("%H:%M:%S"),
                freeze_at.strftime("%H:%M:%S"),
                snapshot_interval_sec,
                float(limit_up_tolerance or 0.0),
            )
            if snapshot_record_path:
                logger.info("%s snapshot_record_path=%s", session_event_prefix, snapshot_record_path)
        else:
            installed = install_observe_strategies(
                runner,
                configs,
                trade_executor=trade_exec,
                position_manager=pos_mgr,
            )
            if full_snapshot_recorder:
                logger.info(
                    "%s full_pool_snapshot_record_start universe=%d scan_start=%s interval_sec=%.1f path=%s",
                    session_event_prefix,
                    full_snapshot_recorder.universe_count,
                    scan_start_at.strftime("%H:%M:%S"),
                    snapshot_interval_sec,
                    snapshot_record_path,
                )
        started = True
        data_thread = threading.Thread(target=data_sub.start, daemon=True, name="opening-auction-data-sub")
        data_thread.start()
        _start_runtime_heartbeat(ctx, stop_event, mode=mode)
        logger.info(
            "%s running strategies=%d installed=%d tick_subscriptions=%d l2_stocks=%d l2=%s",
            session_event_prefix,
            len(runner.get_all_strategies()),
            installed,
            len(data_sub.get_subscription_list()),
            len(data_sub.get_l2_subscription_map()),
            data_sub.get_l2_subscription_map(),
        )

        freeze_logged = False
        while not stop_event.is_set():
            now = now_provider()
            if scanner and scan_start_at <= now < freeze_at:
                try:
                    for candidate in scanner.scan_once(now):
                        installed += install_observe_strategies(
                            runner,
                            build_strategy_configs([candidate]),
                            trade_executor=trade_exec,
                            position_manager=pos_mgr,
                        )
                except Exception as exc:
                    logger.error("%s snapshot_scan_failed error=%s", session_event_prefix, exc, exc_info=True)

            if full_snapshot_recorder and now >= scan_start_at:
                elapsed = (
                    snapshot_interval_sec
                    if last_full_snapshot_at is None
                    else (now - last_full_snapshot_at).total_seconds()
                )
                if elapsed >= snapshot_interval_sec:
                    try:
                        full_snapshot_recorder.record_once(now)
                    except Exception as exc:
                        logger.error("%s full_pool_snapshot_record_failed error=%s", session_event_prefix, exc, exc_info=True)
                    last_full_snapshot_at = now

            if scanner and not freeze_logged and now >= freeze_at:
                scanner.scan_once(now)
                logger.info(
                    "%s candidate_freeze candidates=%d universe=%d freeze_time=%s installed=%d",
                    session_event_prefix,
                    scanner.candidate_count,
                    scanner.universe_count,
                    freeze_at.strftime("%H:%M:%S"),
                    installed,
                )
                freeze_logged = True

            if started and not preopen_reference_emitted and now >= preopen_reference_at:
                ranking_rows, buy_plan_rows = emit_auction_rankings_and_buy_plan(
                    runner,
                    logger=logger,
                    ranking_output_path=ranking_output_path,
                    buy_plan_output_path=buy_plan_output_path,
                    matched_candidates_output_path=matched_candidates_output_path,
                    matched_candidates_md_output_path=matched_candidates_md_output_path,
                    buy_plan_top_n=buy_plan_top_n,
                    buy_plan_min_score=buy_plan_min_score,
                    buy_plan_amount=buy_plan_amount,
                )
                logger.info(
                    "%s preopen_reference_emitted time=%s rows=%d buy_plan_rows=%d ranking_output=%s buy_plan_output=%s matched_candidates_output=%s matched_candidates_md_output=%s observe_only=true real_order_sent=false",
                    session_event_prefix,
                    preopen_reference_at.strftime("%H:%M:%S"),
                    ranking_rows,
                    buy_plan_rows,
                    ranking_output_path,
                    buy_plan_output_path,
                    matched_candidates_output_path,
                    matched_candidates_md_output_path,
                )
                preopen_reference_emitted = True

            if stop_at is not None and now >= stop_at:
                logger.info(
                    "%s session_stop reason=%s stop_time=%s strategy_count=%d tick_subscriptions=%d l2_stocks=%d",
                    session_event_prefix,
                    stop_reason,
                    stop_at.strftime("%H:%M:%S"),
                    len(runner.get_all_strategies()),
                    len(data_sub.get_subscription_list()),
                    len(data_sub.get_l2_subscription_map()),
                )
                stop_event.set()
                break
            if scanner and scan_start_at <= now < freeze_at:
                sleep_fn(snapshot_interval_sec)
            elif full_snapshot_recorder and now >= scan_start_at:
                sleep_fn(snapshot_interval_sec)
            else:
                sleep_fn(1)
    finally:
        emitted = emit_auction_decisions(runner, logger) if started else 0
        logger.info("%s decisions_emitted=%d", session_event_prefix, emitted)
        ranking_rows = 0
        buy_plan_rows = 0
        if started:
            ranking_rows, buy_plan_rows = emit_auction_rankings_and_buy_plan(
                runner,
                logger=logger,
                ranking_output_path=ranking_output_path,
                buy_plan_output_path=buy_plan_output_path,
                matched_candidates_output_path=matched_candidates_output_path,
                matched_candidates_md_output_path=matched_candidates_md_output_path,
                buy_plan_top_n=buy_plan_top_n,
                buy_plan_min_score=buy_plan_min_score,
                buy_plan_amount=buy_plan_amount,
            )
        logger.info(
            "%s ranking_and_plan rows=%d buy_plan_rows=%d observe_only=true real_order_sent=false",
            session_event_prefix,
            ranking_rows,
            buy_plan_rows,
        )
        if scanner:
            scanner.close()
        if full_snapshot_recorder:
            logger.info(
                "%s full_pool_snapshot_record_stop rows=%d path=%s",
                session_event_prefix,
                full_snapshot_recorder.rows_written,
                snapshot_record_path,
            )
            full_snapshot_recorder.close()
        try:
            runner.stop()
        finally:
            data_sub.stop()
        logger.info(
            "%s stopped system_log=%s trade_log=%s dry_run=%s real_order_sent=false",
            session_event_prefix,
            get_log_file_path("system"),
            get_log_file_path("trade"),
            settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run OpeningAuctionAttitude observe-only market session.")
    parser.add_argument("--codes", action="append", default=[], help="Comma/space separated stock codes.")
    parser.add_argument("--pool", default=DEFAULT_POOL, help="CSV stock pool path used when --codes is empty.")
    parser.add_argument("--max-count", type=int, default=0, help="Limit observed symbols, 0 means unlimited.")
    parser.add_argument("--stop-time", default="09:35:00", help="Auto stop time in HH:MM or HH:MM:SS.")
    parser.add_argument("--scan-start-time", default="09:15:00", help="Snapshot scan start time.")
    parser.add_argument("--candidate-freeze-time", default="09:24:30", help="Legacy --dynamic-candidates only: stop adding candidates after this time.")
    parser.add_argument("--snapshot-interval-sec", type=float, default=2.0, help="Full-tick snapshot polling interval in seconds.")
    parser.add_argument("--limit-up-tolerance", type=float, default=0.01, help="Price tolerance for limit-up snapshot hit.")
    parser.add_argument("--snapshot-record-path", default="", help="Optional JSONL path. In default install-all mode records full-pool snapshots; in --dynamic-candidates mode records legacy scanner snapshots.")
    candidate_mode = parser.add_mutually_exclusive_group()
    candidate_mode.add_argument(
        "--install-all",
        dest="install_all",
        action="store_true",
        default=True,
        help="Install strategies for the whole loaded pool at startup (default).",
    )
    candidate_mode.add_argument(
        "--dynamic-candidates",
        dest="install_all",
        action="store_false",
        help="Use legacy snapshot scanner and install only symbols that hit the limit-up condition before freeze.",
    )
    parser.add_argument("--ranking-output", default="", help="CSV path for 09:25 auction ranking output. Empty uses strategy output dir.")
    parser.add_argument("--buy-plan-output", default="", help="CSV path for plan-only buy-plan output. Empty uses strategy output dir.")
    parser.add_argument("--matched-candidates-output", default="", help="Human-readable CSV path for matched auction candidates.")
    parser.add_argument("--matched-candidates-md-output", default="", help="Human-readable Markdown path for matched auction candidates.")
    parser.add_argument("--preopen-reference-time", default="09:25:15", help="Write auction ranking/buy-plan reference once before open.")
    parser.add_argument("--buy-plan-top-n", type=int, default=DEFAULT_BUY_PLAN_TOP_N, help="Maximum plan-only candidates to output. 0 means all matched rows.")
    parser.add_argument("--buy-plan-min-score", type=float, default=DEFAULT_BUY_PLAN_MIN_SCORE, help="Deprecated while condition filtering is active.")
    parser.add_argument("--buy-plan-amount", type=float, default=DEFAULT_BUY_PLAN_AMOUNT, help="Reference plan amount; no order is sent.")
    parser.add_argument("--heartbeat-interval-sec", type=int, default=30)
    parser.add_argument("--full-console", action="store_true", help="Disable summary mode and print all console logs.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = build_observe_settings(args)
    anchor = datetime.now()
    stop_at = build_session_time(anchor, str(args.stop_time))
    ranking_output = str(args.ranking_output or default_output_path("auction_rankings", anchor))
    buy_plan_output = str(args.buy_plan_output or default_output_path("auction_buy_plan", anchor))
    matched_candidates_output = str(args.matched_candidates_output or default_output_path("auction_matched_candidates", anchor))
    matched_candidates_md_output = str(
        args.matched_candidates_md_output
        or str(Path(default_output_path("auction_matched_candidates", anchor)).with_suffix(".md"))
    )
    run_market_only(
        codes=args.codes,
        pool_path=str(args.pool),
        max_count=int(args.max_count),
        settings=settings,
        stop_at=stop_at,
        dynamic_candidates=not bool(args.install_all),
        scan_start_time=str(args.scan_start_time),
        candidate_freeze_time=str(args.candidate_freeze_time),
        snapshot_interval_sec=float(args.snapshot_interval_sec),
        limit_up_tolerance=float(args.limit_up_tolerance),
        snapshot_record_path=str(args.snapshot_record_path),
        ranking_output_path=ranking_output,
        buy_plan_output_path=buy_plan_output,
        matched_candidates_output_path=matched_candidates_output,
        matched_candidates_md_output_path=matched_candidates_md_output,
        preopen_reference_time=str(args.preopen_reference_time),
        buy_plan_top_n=int(args.buy_plan_top_n),
        buy_plan_min_score=float(args.buy_plan_min_score),
        buy_plan_amount=float(args.buy_plan_amount),
    )


if __name__ == "__main__":
    main()
