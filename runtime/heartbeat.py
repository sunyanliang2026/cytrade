from __future__ import annotations

import threading

from monitor.logger import get_logger


def format_dt(value) -> str:
    if not value:
        return ""
    formatter = getattr(value, "strftime", None)
    if callable(formatter):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def start_runtime_heartbeat(ctx: dict, stop_event: threading.Event, mode: str) -> threading.Thread:
    """Log operational heartbeats so quiet markets are distinguishable from hangs."""
    logger = get_logger("system")
    settings = ctx["settings"]
    runner = ctx["runner"]
    data_sub = ctx["data_sub"]
    conn_mgr = ctx.get("conn_mgr")
    interval = max(5, int(getattr(settings, "RUNTIME_HEARTBEAT_INTERVAL_SEC", 30) or 30))
    stable_repeat = 4

    def _loop() -> None:
        last_signature = None
        unchanged_count = 0
        while not stop_event.wait(interval):
            try:
                l2_map = data_sub.get_l2_subscription_map()
                data_status = data_sub.get_latest_data_status()
                runner_status = runner.get_runtime_status() if hasattr(runner, "get_runtime_status") else {}
                conn_last_error = conn_mgr.get_last_error() if conn_mgr and hasattr(conn_mgr, "get_last_error") else {}
                trading_ready = bool(conn_mgr.is_trading_ready()) if conn_mgr and hasattr(conn_mgr, "is_trading_ready") else False
                signature = (
                    bool(getattr(settings, "CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN", True)),
                    bool(conn_mgr.is_connected()) if conn_mgr else False,
                    trading_ready,
                    conn_last_error.get("stage", ""),
                    conn_last_error.get("return_code", ""),
                    runner_status.get("strategy_count", ""),
                    len(data_sub.get_subscription_list()),
                    len(l2_map),
                    sum(len(kinds) for kinds in l2_map.values()),
                    format_dt(data_status.get("latest_data_time")),
                    format_dt(data_status.get("last_recv_time")),
                    round(float(data_status.get("data_delay_ms", 0.0) or 0.0), 0),
                    runner_status.get("last_strategy_event", ""),
                    format_dt(runner_status.get("last_strategy_event_time")),
                )
                changed = signature != last_signature
                if changed:
                    unchanged_count = 0
                else:
                    unchanged_count += 1

                if not changed and unchanged_count < stable_repeat:
                    continue

                heartbeat_reason = "changed" if changed else "stable"
                stable_for_sec = 0 if changed else unchanged_count * interval
                logger.info(
                    (
                        "Runtime heartbeat mode=%s reason=%s stable_for_sec=%d dry_run=%s connected=%s "
                        "trading_ready=%s account_stage=%s account_return_code=%s strategies=%s "
                        "tick_subscriptions=%d l2_stocks=%d l2_kinds=%d latest_data_time=%s "
                        "last_recv_time=%s data_delay_ms=%.0f last_strategy_event=%s "
                        "last_strategy_event_time=%s process_ms=%.1f"
                    ),
                    mode,
                    heartbeat_reason,
                    stable_for_sec,
                    bool(getattr(settings, "CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN", True)),
                    bool(conn_mgr.is_connected()) if conn_mgr else False,
                    trading_ready,
                    conn_last_error.get("stage", ""),
                    conn_last_error.get("return_code", ""),
                    runner_status.get("strategy_count", ""),
                    len(data_sub.get_subscription_list()),
                    len(l2_map),
                    sum(len(kinds) for kinds in l2_map.values()),
                    format_dt(data_status.get("latest_data_time")),
                    format_dt(data_status.get("last_recv_time")),
                    float(data_status.get("data_delay_ms", 0.0) or 0.0),
                    runner_status.get("last_strategy_event", ""),
                    format_dt(runner_status.get("last_strategy_event_time")),
                    float(runner_status.get("last_round_total_process_ms", 0.0) or 0.0),
                )
                last_signature = signature
                if not changed:
                    unchanged_count = 0
            except Exception as exc:
                logger.warning("Runtime heartbeat failed mode=%s error=%s", mode, exc)

    thread = threading.Thread(target=_loop, daemon=True, name=f"runtime-heartbeat-{mode}")
    thread.start()
    return thread
