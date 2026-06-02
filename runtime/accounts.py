from __future__ import annotations

import threading
from collections.abc import Callable

from monitor.logger import get_logger


def log_runtime_startup_config(settings, conn_mgr, mode: str) -> None:
    logger = get_logger("system")
    conn_cfg = conn_mgr.get_startup_config() if hasattr(conn_mgr, "get_startup_config") else {}
    logger.info(
        "Runtime startup mode=%s dry_run=%s qmt_path=%s account_id=%s account_type=%s",
        mode,
        bool(getattr(settings, "CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN", True)),
        conn_cfg.get("qmt_path", getattr(settings, "QMT_PATH", "")),
        conn_cfg.get("account_id", getattr(settings, "ACCOUNT_ID", "")),
        conn_cfg.get("account_type", getattr(settings, "ACCOUNT_TYPE", "")),
    )


def log_connection_failure(conn_mgr, mode: str) -> None:
    logger = get_logger("system")
    last_error = conn_mgr.get_last_error() if hasattr(conn_mgr, "get_last_error") else {}
    logger.error(
        "Runtime startup blocked mode=%s live_trading_allowed=false stage=%s account_id=%s account_type=%s qmt_path=%s return_code=%s error=%s",
        mode,
        last_error.get("stage", "connect"),
        last_error.get("account_id", ""),
        last_error.get("account_type", ""),
        last_error.get("qmt_path", ""),
        last_error.get("return_code", ""),
        last_error.get("error", ""),
    )


def is_dry_run(settings) -> bool:
    return bool(getattr(settings, "CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN", True))


def sync_account_after_connection_recovered(ctx: dict, mode: str) -> None:
    logger = get_logger("system")
    runner = ctx.get("runner")
    if not runner or not hasattr(runner, "sync_orders_and_trades_once"):
        return

    try:
        summary = runner.sync_orders_and_trades_once(reason="account_recovered")
        logger.info("Runtime account recovery sync mode=%s summary=%s", mode, summary)
    except Exception as exc:
        logger.error("Runtime account recovery sync failed mode=%s error=%s", mode, exc, exc_info=True)


def start_account_connection_retry(
    ctx: dict,
    stop_event: threading.Event,
    mode: str,
    *,
    sync_fn: Callable[[dict, str], None] = sync_account_after_connection_recovered,
    log_failure_fn: Callable[[object, str], None] = log_connection_failure,
) -> threading.Thread:
    logger = get_logger("system")
    settings = ctx["settings"]
    conn_mgr = ctx["conn_mgr"]
    retry_interval = max(15, int(getattr(settings, "RUNTIME_HEARTBEAT_INTERVAL_SEC", 30) or 30))

    def _loop() -> None:
        attempt = 0
        while not stop_event.wait(retry_interval):
            if conn_mgr.is_trading_ready():
                logger.info(
                    "Runtime account retry stopped mode=%s reason=already_ready account_id=%s",
                    mode,
                    getattr(settings, "ACCOUNT_ID", ""),
                )
                return

            attempt += 1
            last_error = conn_mgr.get_last_error() if hasattr(conn_mgr, "get_last_error") else {}
            logger.warning(
                (
                    "Runtime account retry attempt mode=%s attempt=%d dry_run=%s "
                    "account_id=%s account_type=%s stage=%s return_code=%s error=%s"
                ),
                mode,
                attempt,
                is_dry_run(settings),
                getattr(settings, "ACCOUNT_ID", ""),
                getattr(settings, "ACCOUNT_TYPE", ""),
                last_error.get("stage", ""),
                last_error.get("return_code", ""),
                last_error.get("error", ""),
            )

            if conn_mgr.connect():
                logger.info(
                    "Runtime account recovered mode=%s attempt=%d account_id=%s account_type=%s trading_ready=%s",
                    mode,
                    attempt,
                    getattr(settings, "ACCOUNT_ID", ""),
                    getattr(settings, "ACCOUNT_TYPE", ""),
                    conn_mgr.is_trading_ready(),
                )
                sync_fn(ctx, mode=mode)
                return

            log_failure_fn(conn_mgr, mode=mode)

    thread = threading.Thread(target=_loop, daemon=True, name=f"account-retry-{mode}")
    thread.start()
    return thread


def validate_live_trading_preflight(ctx: dict, mode: str) -> bool:
    logger = get_logger("system")
    settings = ctx["settings"]
    dry_run = bool(getattr(settings, "CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN", True))
    trade_exec = ctx.get("trade_exec")
    status = (
        trade_exec.get_live_guard_status()
        if trade_exec and hasattr(trade_exec, "get_live_guard_status")
        else {}
    )
    last_error = status.get("last_error") or {}
    logger.info(
        (
            "Live trading preflight mode=%s dry_run=%s live_enabled=%s "
            "xtquant_available=%s has_trader=%s has_account=%s trading_ready=%s "
            "stage=%s return_code=%s error=%s"
        ),
        mode,
        dry_run,
        status.get("live_trading_enabled", False),
        status.get("xtquant_available", False),
        status.get("has_trader", False),
        status.get("has_account", False),
        status.get("trading_ready", False),
        last_error.get("stage", ""),
        last_error.get("return_code", ""),
        last_error.get("error", ""),
    )
    if dry_run:
        return True

    required = (
        status.get("live_trading_enabled") is True
        and status.get("xtquant_available") is True
        and status.get("has_trader") is True
        and status.get("has_account") is True
        and status.get("trading_ready") is True
    )
    if not required:
        logger.error(
            "Runtime startup blocked mode=%s reason=live_trading_preflight_failed status=%s",
            mode,
            status,
        )
        return False

    snapshot = (
        trade_exec.get_live_account_snapshot()
        if trade_exec and hasattr(trade_exec, "get_live_account_snapshot")
        else {"asset_available": False}
    )
    logger.info(
        "Live account preflight mode=%s asset_available=%s available_cash=%s total_asset=%s",
        mode,
        snapshot.get("asset_available", False),
        snapshot.get("available_cash", None),
        snapshot.get("total_asset", None),
    )
    if not snapshot.get("asset_available") or snapshot.get("available_cash") is None:
        logger.error(
            "Runtime startup blocked mode=%s reason=live_account_asset_unavailable snapshot=%s",
            mode,
            snapshot,
        )
        return False
    return True


def connect_account_for_runtime(
    ctx: dict,
    mode: str,
    stop_event: threading.Event | None = None,
    *,
    start_retry_fn: Callable[[dict, threading.Event, str], threading.Thread] = start_account_connection_retry,
    validate_fn: Callable[[dict, str], bool] = validate_live_trading_preflight,
    log_startup_fn: Callable[[object, object, str], None] = log_runtime_startup_config,
    log_failure_fn: Callable[[object, str], None] = log_connection_failure,
) -> bool:
    logger = get_logger("system")
    settings = ctx["settings"]
    conn_mgr = ctx["conn_mgr"]
    dry_run = is_dry_run(settings)

    log_startup_fn(settings, conn_mgr, mode=mode)
    if conn_mgr.connect():
        return validate_fn(ctx, mode=mode)

    log_failure_fn(conn_mgr, mode=mode)
    if not dry_run:
        logger.error("Unable to connect QMT; live trading is disabled and runtime exits")
        return False

    logger.warning(
        (
            "Runtime account unavailable at startup mode=%s dry_run=%s action=continue_market_monitor_and_retry "
            "account_id=%s account_type=%s"
        ),
        mode,
        dry_run,
        getattr(settings, "ACCOUNT_ID", ""),
        getattr(settings, "ACCOUNT_TYPE", ""),
    )
    if stop_event is not None:
        start_retry_fn(ctx, stop_event, mode=mode)
    return True
