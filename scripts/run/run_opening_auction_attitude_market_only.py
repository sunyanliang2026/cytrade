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
from typing import Callable, Iterable, Optional

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import Settings
from main import _log_runtime_startup_config, _start_runtime_heartbeat, build_app
from monitor.logger import get_log_file_path, get_logger
from strategy.models import StrategyConfig
from strategy.opening_auction_attitude import OpeningAuctionAttitudeStrategy


DEFAULT_POOL = "data/stock_pools/current/opening_auction_universe.csv"
SESSION_EVENT_PREFIX = "OPENING_AUCTION_ATTITUDE_SESSION"
EVENT_NAME = "MSF_AUCTION_ATTITUDE"

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
) -> None:
    entries = resolve_observe_entries(codes=codes, pool_path=pool_path, max_count=max_count)
    configs = build_strategy_configs(entries)
    if not configs:
        raise SystemExit("No opening-auction observe symbols. Use --codes or a valid --pool file.")

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
        ",".join(config.stock_code for config in configs),
        max_count,
        stop_at.strftime("%H:%M:%S") if stop_at else "",
    )
    _log_runtime_startup_config(settings, conn_mgr, mode=mode)

    started = False
    try:
        runner.start()
        installed = install_observe_strategies(
            runner,
            configs,
            trade_executor=trade_exec,
            position_manager=pos_mgr,
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

        while not stop_event.is_set():
            if stop_at is not None and now_provider() >= stop_at:
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
            sleep_fn(1)
    finally:
        emitted = emit_auction_decisions(runner, logger) if started else 0
        logger.info("%s decisions_emitted=%d", session_event_prefix, emitted)
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
    parser.add_argument("--heartbeat-interval-sec", type=int, default=30)
    parser.add_argument("--full-console", action="store_true", help="Disable summary mode and print all console logs.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = build_observe_settings(args)
    stop_at = build_session_time(datetime.now(), str(args.stop_time))
    run_market_only(
        codes=args.codes,
        pool_path=str(args.pool),
        max_count=int(args.max_count),
        settings=settings,
        stop_at=stop_at,
    )


if __name__ == "__main__":
    main()
