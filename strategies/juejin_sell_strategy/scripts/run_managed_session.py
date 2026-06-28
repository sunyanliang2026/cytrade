"""Run JuejinSellStrategy from ``sell_10.csv`` in a managed session.

The strategy reads sell quantity from CSV and intentionally does not require a
matching live account holding before it emits a sell attempt.  Whether the order
is accepted or rejected is left to the shared execution/account layer.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import Settings
from main import run_daily_session
from monitor.logger import get_log_file_path, get_logger
from strategies.juejin_sell_strategy import JuejinSellStrategy
from runtime.session import build_session_time

SESSION_EVENT_PREFIX = "JUEJIN_SELL_SESSION"


def _default_csv_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "sell_10.csv"


def build_managed_settings(args: argparse.Namespace) -> Settings:
    """Build runtime settings without forcing dry-run/mock mode on or off."""
    csv_path = Path(args.csv).expanduser().resolve()
    overrides = {
        # Reuse the existing runtime field for startup logs; the strategy
        # itself reads CYTRADE_JUEJIN_SELL_CSV_PATH set by run_managed_session().
        "CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH": str(csv_path),
        "LOG_SUMMARY_MODE": not bool(args.full_console),
        "SESSION_START_TIME": str(args.strategy_start_time),
        "SESSION_EXIT_TIME": str(args.stop_time),
        "LOAD_PREVIOUS_STATE_ON_START": False,
    }
    if int(args.heartbeat_interval_sec) > 0:
        overrides["RUNTIME_HEARTBEAT_INTERVAL_SEC"] = int(args.heartbeat_interval_sec)
    return Settings(**overrides)


def run_managed_session(args: argparse.Namespace) -> str:
    logger = get_logger("system")
    csv_path = Path(args.csv).expanduser().resolve()
    if not csv_path.is_file():
        logger.error("%s skipped reason=csv_missing csv=%s", SESSION_EVENT_PREFIX, csv_path)
        return "skipped_missing_csv"

    now = datetime.now()
    stop_at = build_session_time(now, str(args.stop_time))
    if now >= stop_at:
        logger.info("%s skipped reason=after_stop_time stop_time=%s", SESSION_EVENT_PREFIX, args.stop_time)
        return "skipped_after_stop"

    os.environ["CYTRADE_JUEJIN_SELL_CSV_PATH"] = str(csv_path)
    runtime_settings = build_managed_settings(args)
    dry_run = bool(runtime_settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN)
    logger.info(
        (
            "%s managed_start csv=%s strategy_start_time=%s stop_time=%s "
            "dry_run=%s live_enabled=%s summary_mode=%s system_log=%s trade_log=%s"
        ),
        SESSION_EVENT_PREFIX,
        csv_path,
        runtime_settings.SESSION_START_TIME,
        runtime_settings.SESSION_EXIT_TIME,
        dry_run,
        not dry_run,
        runtime_settings.LOG_SUMMARY_MODE,
        get_log_file_path("system"),
        get_log_file_path("trade"),
    )
    result = run_daily_session(strategy_classes=[JuejinSellStrategy], settings=runtime_settings)
    logger.info("%s managed_stopped result=%s csv=%s", SESSION_EVENT_PREFIX, result, csv_path)
    return str(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run JuejinSellStrategy managed session from sell_10.csv.")
    parser.add_argument("--csv", default=str(_default_csv_path()), help="CSV file with symbol,exp,sellvol,nick columns.")
    parser.add_argument("--strategy-start-time", default="09:15", help="Strategy runtime start time in HH:MM.")
    parser.add_argument("--stop-time", default="15:05", help="Session stop time in HH:MM.")
    parser.add_argument("--heartbeat-interval-sec", type=int, default=30)
    parser.add_argument("--full-console", action="store_true", help="Disable summary mode and print all console logs.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    run_managed_session(args)


if __name__ == "__main__":
    main()
