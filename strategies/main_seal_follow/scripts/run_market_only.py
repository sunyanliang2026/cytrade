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
from datetime import datetime
from pathlib import Path
from typing import Optional

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import _log_runtime_startup_config, _start_runtime_heartbeat, build_app
from config.settings import Settings
from monitor.logger import get_log_file_path, get_logger
from strategies.main_seal_follow import MainSealFollowStrategy


def _apply_runtime_settings(runtime_settings) -> None:
    from config.settings import settings as global_runtime_settings

    for name in dir(runtime_settings):
        if not name.isupper():
            continue
        try:
            setattr(global_runtime_settings, name, getattr(runtime_settings, name))
        except Exception:
            continue


def run_market_only(
    *,
    settings=None,
    stop_at: Optional[datetime] = None,
    mode: str = "market-only",
    session_event_prefix: str = "",
    stop_reason: str = "scheduled_stop",
) -> None:
    if settings is None:
        settings = Settings(LOAD_PREVIOUS_STATE_ON_START=False)
    else:
        settings.LOAD_PREVIOUS_STATE_ON_START = False

    if settings is not None:
        _apply_runtime_settings(settings)

    ctx = build_app(strategy_classes=[MainSealFollowStrategy], settings=settings)
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
    if session_event_prefix:
        logger.info(
            "%s session_start mode=%s dry_run=%s csv=%s stop_at=%s account_connected=false",
            session_event_prefix,
            mode,
            settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN,
            settings.CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH,
            stop_at.strftime("%H:%M") if stop_at else "",
        )
    _log_runtime_startup_config(settings, conn_mgr, mode=mode)

    try:
        runner.start()
        data_thread = threading.Thread(target=data_sub.start, daemon=True, name="data-sub")
        data_thread.start()
        _start_runtime_heartbeat(ctx, stop_event, mode=mode)
        logger.info(
            "MainSealFollow market-only dry-run session running strategies=%d l2=%s",
            len(runner.get_all_strategies()),
            data_sub.get_l2_subscription_map(),
        )
        while not stop_event.is_set():
            if stop_at is not None and datetime.now() >= stop_at:
                if session_event_prefix:
                    logger.info(
                        "%s session_stop reason=%s stop_time=%s strategy_count=%d tick_subscriptions=%d l2_stocks=%d",
                        session_event_prefix,
                        stop_reason,
                        stop_at.strftime("%H:%M"),
                        len(runner.get_all_strategies()),
                        len(data_sub.get_subscription_list()),
                        len(data_sub.get_l2_subscription_map()),
                    )
                stop_event.set()
                break
            time.sleep(1)
    finally:
        try:
            runner.stop()
        finally:
            data_sub.stop()
        if session_event_prefix:
            logger.info(
                "%s stopped system_log=%s trade_log=%s dry_run=%s real_order_sent=false",
                session_event_prefix,
                get_log_file_path("system"),
                get_log_file_path("trade"),
                settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN,
            )
        logger.info("MainSealFollow market-only dry-run session stopped")


if __name__ == "__main__":
    run_market_only()
