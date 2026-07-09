"""cytrade main entry point.

This module assembles the runtime container and delegates session lifecycle work to the runtime package. The parent process keeps only the scheduler alive; each trading-day session runs in a child process so QMT connections, subscriptions, strategies, web services, and watchdog state can be released cleanly after the session exits."""
import sys
import os
import signal
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

# Ensure the project root is importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.xtquant_bootstrap import bootstrap_xtquant_sys_path

XTQUANT_BOOTSTRAP_ROOT = bootstrap_xtquant_sys_path()

from config.settings import Settings
from config.fee_schedule import FeeSchedule
from monitor.logger import LogManager, get_logger
from data.manager import DataManager
from core.connection import ConnectionManager
from core.callback import MyXtQuantTraderCallback
from core.data_subscription import DataSubscriptionManager
from core.trading_calendar import is_market_day
from trading.order_manager import OrderManager
from trading.executor import TradeExecutor
from position.manager import PositionManager
from strategy.runner import StrategyRunner
from monitor.watchdog import Watchdog
from runtime.heartbeat import format_dt as _format_dt
from runtime.heartbeat import start_runtime_heartbeat as _start_runtime_heartbeat
from runtime import accounts as _runtime_accounts
from runtime import session as _runtime_session
from runtime import strategies as _runtime_strategies


STRATEGY_SELECTION_SETTING_KEYS = (
    "CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH",
    "CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN",
    "CYTRADE_MAIN_SEAL_FOLLOW_L2_CALIBRATION",
    "CYTRADE_MAIN_SEAL_FOLLOW_L2_CALIBRATION_DIR",
    "LOG_DIR",
)


def _build_strategy_selection_overrides(settings: Settings) -> dict:
    return {
        name: getattr(settings, name)
        for name in STRATEGY_SELECTION_SETTING_KEYS
        if hasattr(settings, name)
    }


def _to_strategy_spec(strategy_class_or_spec) -> str:
    return _runtime_strategies.to_strategy_spec(strategy_class_or_spec)


def _normalize_strategy_specs(strategy_classes=None) -> list[str]:
    return _runtime_strategies.normalize_strategy_specs(strategy_classes)


def _resolve_strategy_specs(strategy_specs) -> list[type]:
    return _runtime_strategies.resolve_strategy_specs(strategy_specs)


def build_app(strategy_classes=None, settings: Settings = None):
    """Build and wire the core runtime objects.

    The function performs dependency wiring only. It does not connect to QMT, subscribe market data, or start strategy execution. The returned context is reused by live sessions and tests."""
    settings = settings or Settings()
    # Prepare runtime directories before logs, SQLite, and state files are written.
    settings.ensure_dirs()

    # ---- Logging ----
    log_mgr = LogManager(
        log_dir=settings.LOG_DIR,
        max_days=settings.LOG_MAX_DAYS,
        level=settings.LOG_LEVEL,
        summary_mode=settings.LOG_SUMMARY_MODE,
    )
    logger = get_logger("system")
    logger.info("=" * 50)
    logger.info("cytrade 启动")
    if XTQUANT_BOOTSTRAP_ROOT:
        logger.info("cytrade: xtquant root=%s", XTQUANT_BOOTSTRAP_ROOT)

    # ---- Data manager ----
    data_mgr = DataManager(
        db_path=settings.SQLITE_DB_PATH,
        state_dir=settings.STATE_SAVE_DIR,
        remote_cfg=settings.REMOTE_DB_CONFIG,
    )
    if settings.ENABLE_REMOTE_DB:
        data_mgr.set_remote_enabled(True)

    # FeeSchedule centralizes commission, stamp tax, and T+0 rules.
    fee_schedule = FeeSchedule(
        file_path=settings.FEE_TABLE_PATH,
        default_buy_fee_rate=settings.DEFAULT_BUY_FEE_RATE,
        default_sell_fee_rate=settings.DEFAULT_SELL_FEE_RATE,
        default_stamp_tax_rate=settings.DEFAULT_STAMP_TAX_RATE,
    )

    # ---- Trading connection ----
    conn_mgr = ConnectionManager(
        qmt_path=settings.QMT_PATH,
        account_id=settings.ACCOUNT_ID,
        account_type=settings.ACCOUNT_TYPE,
        base_interval=settings.RECONNECT_BASE_SEC,
        max_interval=settings.RECONNECT_MAX_INTERVAL_SEC,
        max_retries=(settings.RECONNECT_MAX_RETRIES
                     if settings.RECONNECT_MAX_RETRIES > 0 else None),
    )

    # ---- Order manager ----
    order_mgr = OrderManager(data_manager=data_mgr, fee_schedule=fee_schedule)

    # ---- Position manager ----
    pos_mgr = PositionManager(
        cost_method=settings.COST_METHOD,
        data_manager=data_mgr,
        fee_schedule=fee_schedule,
    )

    # ---- Register callback chain: trades -> positions ----
    order_mgr.set_position_callback(pos_mgr.on_trade_callback)

    # ---- Trade executor ----
    trade_exec = TradeExecutor(
        conn_mgr,
        order_mgr,
        pos_mgr,
        live_trading_enabled=not bool(settings.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN),
    )

    # ---- XtQuant callback ----
    callback = MyXtQuantTraderCallback(
        order_manager=order_mgr,
        connection_manager=conn_mgr,
    )
    conn_mgr.register_callback(callback)

    # ---- Data subscription ----
    # Keep market data subscription independent from the trading connection for reconnect recovery.
    data_sub = DataSubscriptionManager(
        latency_threshold_sec=settings.DATA_LATENCY_THRESHOLD_SEC,
        default_period=settings.SUBSCRIPTION_PERIOD,
        print_latest_status=not bool(settings.LOG_SUMMARY_MODE),
    )

    # ---- Strategy runner ----
    runner = StrategyRunner(
        data_subscription=data_sub,
        trade_executor=trade_exec,
        order_manager=order_mgr,
        position_manager=pos_mgr,
        data_manager=data_mgr,
        connection_manager=conn_mgr,
        strategy_classes=strategy_classes or [],
        load_previous_state_on_start=settings.LOAD_PREVIOUS_STATE_ON_START,
        latency_threshold_sec=settings.DATA_LATENCY_THRESHOLD_SEC,
        process_threshold_ms=settings.STRATEGY_PROCESS_THRESHOLD_MS,
        selection_runtime_overrides=_build_strategy_selection_overrides(settings),
    )

    # Forward order status changes to the owning strategy instance.
    # Strategies use this to update internal state after fills, cancels, and rejects.
    order_mgr.set_strategy_callback(runner.dispatch_order_update)

    # ConnectionManager handles reconnects after network interruptions.
    # Resubscribe market data after reconnect succeeds.
    conn_mgr.register_reconnect_callback(data_sub.resubscribe_all)

    # ---- Watchdog ----
    watchdog = Watchdog(
        interval_sec=settings.WATCHDOG_INTERVAL_SEC,
        dingtalk_webhook=settings.DINGTALK_WEBHOOK_URL,
        dingtalk_secret=settings.DINGTALK_SECRET,
        cpu_threshold=settings.CPU_ALERT_THRESHOLD,
        mem_threshold=settings.MEM_ALERT_THRESHOLD,
        position_report_times=settings.POSITION_REPORT_TIMES,
        position_manager=pos_mgr,
        connection_manager=conn_mgr,
        data_subscription=data_sub,
    )

    # Refresh watchdog heartbeat whenever market data reaches the runner.
    runner.set_heartbeat_callback(watchdog.register_heartbeat)
    runner.set_alert_callback(watchdog.send_dingtalk_alert)

    # Return the wired runtime context for live sessions and tests.
    # 1. Reused directly by run().
    # 2. Lets tests assert module wiring precisely.
    return {
        "settings": settings,
        "log_mgr": log_mgr,
        "data_mgr": data_mgr,
        "fee_schedule": fee_schedule,
        "conn_mgr": conn_mgr,
        "order_mgr": order_mgr,
        "pos_mgr": pos_mgr,
        "trade_exec": trade_exec,
        "callback": callback,
        "data_sub": data_sub,
        "runner": runner,
        "watchdog": watchdog,
    }




def _log_runtime_startup_config(settings: Settings, conn_mgr: ConnectionManager, mode: str) -> None:
    return _runtime_accounts.log_runtime_startup_config(settings, conn_mgr, mode)


def _log_connection_failure(conn_mgr: ConnectionManager, mode: str) -> None:
    return _runtime_accounts.log_connection_failure(conn_mgr, mode)


def _is_dry_run(settings: Settings) -> bool:
    return _runtime_accounts.is_dry_run(settings)


def _sync_account_after_connection_recovered(ctx: dict, mode: str) -> None:
    return _runtime_accounts.sync_account_after_connection_recovered(ctx, mode)


def _start_account_connection_retry(ctx: dict, stop_event: threading.Event, mode: str) -> threading.Thread:
    return _runtime_accounts.start_account_connection_retry(
        ctx,
        stop_event,
        mode,
        sync_fn=_sync_account_after_connection_recovered,
        log_failure_fn=_log_connection_failure,
    )


def _validate_live_trading_preflight(ctx: dict, mode: str) -> bool:
    return _runtime_accounts.validate_live_trading_preflight(ctx, mode)


def _connect_account_for_runtime(ctx: dict, mode: str, stop_event: threading.Event | None = None) -> bool:
    return _runtime_accounts.connect_account_for_runtime(
        ctx,
        mode,
        stop_event,
        start_retry_fn=_start_account_connection_retry,
        validate_fn=_validate_live_trading_preflight,
        log_startup_fn=_log_runtime_startup_config,
        log_failure_fn=_log_connection_failure,
    )


def _parse_hhmm(value: str) -> tuple[int, int]:
    return _runtime_session.parse_hhmm(value)


def _build_session_time(anchor: datetime, hhmm: str) -> datetime:
    return _runtime_session.build_session_time(anchor, hhmm)


def _wait_until_session_start(settings: Settings, stop_event: threading.Event, now_provider=None, sleep_fn=None) -> bool:
    return _runtime_session.wait_until_session_start(settings, stop_event, now_provider, sleep_fn)


def run(strategy_classes=None, settings: Settings = None):
    return _runtime_session.run_live(
        build_app_fn=build_app,
        connect_account_fn=_connect_account_for_runtime,
        start_heartbeat_fn=_start_runtime_heartbeat,
        strategy_classes=strategy_classes,
        settings=settings,
    )


def _run_managed_session(strategy_classes=None, settings: Settings = None, now_provider=None, sleep_fn=None):
    return _runtime_session.run_managed_session(
        build_app_fn=build_app,
        connect_account_fn=_connect_account_for_runtime,
        start_heartbeat_fn=_start_runtime_heartbeat,
        strategy_classes=strategy_classes,
        settings=settings,
        now_provider=now_provider,
        sleep_fn=sleep_fn,
    )


def run_daily_session(strategy_classes=None, settings: Settings = None, now_provider=None, sleep_fn=None):
    return _runtime_session.run_daily_session(
        run_managed_session_fn=_run_managed_session,
        strategy_classes=strategy_classes,
        settings=settings,
        now_provider=now_provider,
        sleep_fn=sleep_fn,
    )


def _run_daily_session_in_subprocess(strategy_specs: list[str], settings: Settings) -> str:
    return _runtime_session.run_daily_session_in_subprocess(
        strategy_specs,
        settings,
        run_daily_session_fn=run_daily_session,
    )


def _launch_session_in_process(strategy_classes=None, settings: Settings = None) -> str:
    return _runtime_session.launch_session_in_process(
        run_daily_session_in_subprocess_fn=_run_daily_session_in_subprocess,
        strategy_classes=strategy_classes,
        settings=settings,
    )


def run_scheduler_service(strategy_classes=None, settings: Settings = None, scheduler_cls=None) -> None:
    return _runtime_session.run_scheduler_service(
        launch_session_fn=_launch_session_in_process,
        strategy_classes=strategy_classes,
        settings=settings,
        scheduler_cls=scheduler_cls,
    )


if __name__ == "__main__":
    from strategy.test_grid_strategy import TestGridStrategy
    run_scheduler_service(strategy_classes=[TestGridStrategy])
