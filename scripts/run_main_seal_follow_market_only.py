"""Run MainSealFollow in market-only dry-run mode.

This entry point intentionally does not connect the trading account. It is for
validating QMT market data, Level2 subscriptions, and strategy events after the
full live-trading startup path has been blocked or intentionally disabled.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import _log_runtime_startup_config, _start_runtime_heartbeat, build_app
from monitor.logger import get_logger
from strategy.main_seal_follow_strategy import MainSealFollowStrategy


def run_market_only() -> None:
    ctx = build_app(strategy_classes=[MainSealFollowStrategy])
    logger = get_logger("system")
    settings = ctx["settings"]
    conn_mgr = ctx["conn_mgr"]
    runner = ctx["runner"]
    data_sub = ctx["data_sub"]
    stop_event = threading.Event()

    def _stop(sig=None, frame=None) -> None:
        logger.info("MainSealFollow market-only session stopping sig=%s", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    logger.info(
        "MainSealFollow market-only dry-run session starting csv=%s dry_run=%s",
        settings.CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH,
        settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN,
    )
    _log_runtime_startup_config(settings, conn_mgr, mode="market-only")

    try:
        runner.start()
        data_thread = threading.Thread(target=data_sub.start, daemon=True, name="data-sub")
        data_thread.start()
        _start_runtime_heartbeat(ctx, stop_event, mode="market-only")
        logger.info(
            "MainSealFollow market-only dry-run session running strategies=%d l2=%s",
            len(runner.get_all_strategies()),
            data_sub.get_l2_subscription_map(),
        )
        while not stop_event.is_set():
            time.sleep(1)
    finally:
        try:
            runner.stop()
        finally:
            data_sub.stop()
        logger.info("MainSealFollow market-only dry-run session stopped")


if __name__ == "__main__":
    run_market_only()
