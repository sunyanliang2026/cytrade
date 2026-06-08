"""Run a managed MainSealFollow session with separated pool and runtime times.

This wrapper is for full managed runtime orchestration:
1. Wait until the configured pool generation time.
2. Generate the stock pool CSV, or reuse an existing CSV when requested.
3. Hand off to the managed runtime with an independent strategy start time.
4. Stop automatically at the configured session end time.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import Settings
from core.trading_calendar import is_market_day
from main import run_daily_session
from monitor.logger import get_log_file_path, get_logger
from scripts.run.run_main_seal_follow_monitor_session import (
    SESSION_EVENT_PREFIX,
    build_session_time,
    collect_or_reuse_pool,
    resolve_runtime_start_time,
    should_collect_pool,
    wait_until,
)
from scripts.pool.collect_main_seal_pool import DEFAULT_OUTPUT, DEFAULT_SOURCE_CONFIG
from strategy.main_seal_follow_strategy import MainSealFollowStrategy


def build_managed_settings(args: argparse.Namespace) -> Settings:
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


def run_managed_session(args: argparse.Namespace) -> str:
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

        pool_outcome = collect_or_reuse_pool(args, logger)
        if pool_outcome.status == "failed":
            return "skipped_pool_collect_failed"
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

    runtime_settings = build_managed_settings(args)
    logger.info(
        "%s managed_start csv=%s strategy_start_time=%s stop_time=%s dry_run=%s summary_mode=%s system_log=%s trade_log=%s",
        SESSION_EVENT_PREFIX,
        runtime_settings.CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH,
        runtime_settings.SESSION_START_TIME,
        runtime_settings.SESSION_EXIT_TIME,
        runtime_settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN,
        runtime_settings.LOG_SUMMARY_MODE,
        get_log_file_path("system"),
        get_log_file_path("trade"),
    )
    result = run_daily_session(strategy_classes=[MainSealFollowStrategy], settings=runtime_settings)
    logger.info(
        "%s managed_stopped result=%s csv=%s dry_run=%s",
        SESSION_EVENT_PREFIX,
        result,
        runtime_settings.CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH,
        runtime_settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN,
    )
    return str(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run MainSealFollow managed session with separated pool/runtime times.")
    parser.add_argument("--pool-time", default="08:50", help="Stock-pool generation time in HH:MM.")
    parser.add_argument("--strategy-start-time", default="09:15", help="Strategy runtime start time in HH:MM.")
    parser.add_argument("--stop-time", default="23:00", help="Session stop time in HH:MM.")
    parser.add_argument("--pool-source", choices=("combined", "iwencai", "qmt", "jiuyangongshe"), default="combined")
    parser.add_argument("--pool-output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--pool-source-config", default=str(DEFAULT_SOURCE_CONFIG))
    parser.add_argument("--amount", type=float, default=50000.0, help="Planned amount per stock in the output CSV.")
    parser.add_argument("--max-count", type=int, default=0, help="Maximum stock count, 0 means unlimited.")
    parser.add_argument("--heartbeat-interval-sec", type=int, default=30)
    parser.add_argument("--pool-collect-timeout-sec", type=int, default=600, help="Maximum seconds to wait for stock-pool collection. 0 disables the guard.")
    parser.add_argument("--no-pool-fallback", action="store_true", help="Do not reuse the existing pool CSV when collection fails or times out.")
    parser.add_argument("--strict-sources", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--skip-pool-collect", action="store_true", help="Skip auto collection and reuse the existing pool CSV.")
    parser.add_argument("--full-console", action="store_true", help="Disable summary mode and print all console logs.")
    parser.add_argument("--no-market-day-check", dest="market_day_only", action="store_false")
    parser.set_defaults(market_day_only=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    run_managed_session(args)


if __name__ == "__main__":
    main()
