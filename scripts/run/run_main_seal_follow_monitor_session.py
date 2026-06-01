"""Run the next-trading-day MainSealFollow monitoring session.

This wrapper is for dry-run monitoring only:
1. Wait until the configured pool generation time.
2. Generate the stock pool CSV, or reuse an existing CSV when requested.
3. Wait until the configured strategy start time.
4. Start the market-only runtime.
5. Stop automatically at the configured session end time.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import Settings
from core.trading_calendar import is_market_day
from monitor.logger import get_log_file_path, get_logger
from scripts.pool.collect_main_seal_pool import DEFAULT_OUTPUT, DEFAULT_SOURCE_CONFIG, build_parser as build_pool_parser, collect_once
from scripts.run.run_main_seal_follow_market_only import run_market_only

SESSION_EVENT_PREFIX = "MONITOR_SESSION"


def parse_hhmm(value: str) -> tuple[int, int]:
    text = str(value or "").strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except Exception as exc:
        raise argparse.ArgumentTypeError(f"invalid time format: {value!r}, expected HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise argparse.ArgumentTypeError(f"invalid time range: {value!r}")
    return hour, minute


def build_session_time(anchor: datetime, hhmm: str) -> datetime:
    hour, minute = parse_hhmm(hhmm)
    return anchor.replace(hour=hour, minute=minute, second=0, microsecond=0)


def wait_until(target: datetime, logger, label: str) -> None:
    while True:
        now = datetime.now()
        if now >= target:
            return
        remaining = max(0.0, (target - now).total_seconds())
        logger.info(
            "%s waiting phase=%s target_time=%s remaining_sec=%.0f",
            SESSION_EVENT_PREFIX,
            label,
            target.strftime("%H:%M"),
            remaining,
        )
        time.sleep(min(30.0, max(1.0, remaining)))


def build_pool_args(args: argparse.Namespace) -> argparse.Namespace:
    pool_args = build_pool_parser().parse_args(["--once"])
    pool_args.source = str(args.pool_source)
    pool_args.output = str(Path(args.pool_output))
    pool_args.source_config = str(Path(args.pool_source_config))
    pool_args.amount = float(args.amount)
    pool_args.max_count = int(args.max_count)
    pool_args.no_backup = bool(args.no_backup)
    pool_args.strict_sources = bool(args.strict_sources)
    pool_args.market_day_only = bool(args.market_day_only)
    return pool_args


def build_monitor_settings(args: argparse.Namespace) -> Settings:
    overrides = {
        "CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH": str(Path(args.pool_output).resolve()),
        "CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN": True,
        "LOG_SUMMARY_MODE": not bool(args.full_console),
        "SESSION_START_TIME": resolve_runtime_start_time(args),
        "SESSION_EXIT_TIME": str(args.stop_time),
        "LOAD_PREVIOUS_STATE_ON_START": False,
    }
    if int(args.heartbeat_interval_sec) > 0:
        overrides["RUNTIME_HEARTBEAT_INTERVAL_SEC"] = int(args.heartbeat_interval_sec)
    return Settings(**overrides)


def should_collect_pool(args: argparse.Namespace) -> bool:
    return not bool(getattr(args, "skip_pool_collect", False))


def resolve_runtime_start_time(args: argparse.Namespace) -> str:
    return str(getattr(args, "strategy_start_time", "") or getattr(args, "pool_time", "") or "").strip()


def run_monitor_session(args: argparse.Namespace) -> str:
    logger = get_logger("system")
    now = datetime.now()
    if args.market_day_only and not is_market_day(now):
        logger.info("%s skipped reason=non_trading_day date=%s", SESSION_EVENT_PREFIX, now.strftime("%Y-%m-%d"))
        return "skipped_non_trading_day"

    stop_at = build_session_time(now, str(args.stop_time))
    if now >= stop_at:
        logger.info("%s skipped reason=after_stop_time stop_time=%s", SESSION_EVENT_PREFIX, args.stop_time)
        return "skipped_after_stop"

    if should_collect_pool(args):
        pool_at = build_session_time(now, str(args.pool_time))
        if now < pool_at:
            wait_until(pool_at, logger, label="pool_wait")

        pool_args = build_pool_args(args)
        logger.info(
            "%s pool_collect_start source=%s output=%s source_config=%s amount=%s",
            SESSION_EVENT_PREFIX,
            pool_args.source,
            pool_args.output,
            pool_args.source_config,
            pool_args.amount,
        )
        pool_count = collect_once(pool_args)
        logger.info(
            "%s pool_generated output=%s source=%s total=%d amount=%s",
            SESSION_EVENT_PREFIX,
            pool_args.output,
            pool_args.source,
            pool_count,
            pool_args.amount,
        )
    else:
        pool_path = Path(args.pool_output)
        if not pool_path.is_file():
            logger.error("%s skipped reason=pool_file_missing csv=%s", SESSION_EVENT_PREFIX, pool_path)
            return "skipped_missing_pool_file"
        logger.info(
            "%s pool_reused csv=%s source=manual total=unchanged amount=%s",
            SESSION_EVENT_PREFIX,
            pool_path,
            args.amount,
        )

    runtime_start_time = resolve_runtime_start_time(args)
    runtime_at = build_session_time(datetime.now(), runtime_start_time)
    if datetime.now() < runtime_at:
        wait_until(runtime_at, logger, label="runtime_wait")

    runtime_settings = build_monitor_settings(args)
    logger.info(
        "%s monitor_start csv=%s strategy_start_time=%s stop_time=%s dry_run=%s summary_mode=%s system_log=%s trade_log=%s",
        SESSION_EVENT_PREFIX,
        runtime_settings.CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH,
        runtime_start_time,
        args.stop_time,
        runtime_settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN,
        runtime_settings.LOG_SUMMARY_MODE,
        get_log_file_path("system"),
        get_log_file_path("trade"),
    )
    run_market_only(
        settings=runtime_settings,
        stop_at=stop_at,
        mode="market-only-monitor",
        session_event_prefix=SESSION_EVENT_PREFIX,
        stop_reason="scheduled_stop",
    )
    return "completed"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run MainSealFollow dry-run monitoring session.")
    parser.add_argument("--pool-time", default="08:50", help="Stock-pool generation time in HH:MM.")
    parser.add_argument("--strategy-start-time", default="", help="Strategy runtime start time in HH:MM. Defaults to pool-time.")
    parser.add_argument("--stop-time", default="10:00", help="Session stop time in HH:MM.")
    parser.add_argument("--pool-source", choices=("combined", "iwencai", "qmt", "jiuyangongshe"), default="combined")
    parser.add_argument("--pool-output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--pool-source-config", default=str(DEFAULT_SOURCE_CONFIG))
    parser.add_argument("--amount", type=float, default=50000.0, help="Planned amount per stock in the output CSV.")
    parser.add_argument("--max-count", type=int, default=0, help="Maximum stock count, 0 means unlimited.")
    parser.add_argument("--heartbeat-interval-sec", type=int, default=30)
    parser.add_argument("--strict-sources", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--skip-pool-collect", action="store_true", help="Skip auto collection and reuse the existing pool CSV.")
    parser.add_argument("--full-console", action="store_true", help="Disable summary mode and print all console logs.")
    parser.add_argument("--no-market-day-check", dest="market_day_only", action="store_false")
    parser.set_defaults(market_day_only=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    run_monitor_session(args)


if __name__ == "__main__":
    main()
