from __future__ import annotations

import signal
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from collections.abc import Callable

from config.settings import Settings
from core.trading_calendar import is_market_day
from monitor.logger import get_logger
from runtime.strategies import normalize_strategy_specs, resolve_strategy_specs


def parse_hhmm(value: str) -> tuple[int, int]:
    text = str(value or "").strip()
    try:
        hour_str, minute_str = text.split(":", 1)
        hour = int(hour_str)
        minute = int(minute_str)
    except Exception as exc:
        raise ValueError(f"Invalid time format: {value!r}, expected HH:MM") from exc

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid time range: {value!r}")
    return hour, minute


def build_session_time(anchor: datetime, hhmm: str) -> datetime:
    hour, minute = parse_hhmm(hhmm)
    return anchor.replace(hour=hour, minute=minute, second=0, microsecond=0)


def wait_until_session_start(
    settings: Settings,
    stop_event: threading.Event,
    now_provider=None,
    sleep_fn=None,
) -> bool:
    now_provider = now_provider or datetime.now
    sleep_fn = sleep_fn or time.sleep

    while not stop_event.is_set():
        now = now_provider()
        start_at = build_session_time(now, settings.SESSION_START_TIME)
        if now >= start_at:
            return True

        remaining = max(0.0, (start_at - now).total_seconds())
        wait_seconds = min(max(int(settings.SESSION_POLL_INTERVAL_SEC), 1), remaining)
        get_logger("system").info("距离会话启动还有 %.0f 秒，继续等待", remaining)
        sleep_fn(wait_seconds)

    return False


def run_live(
    *,
    build_app_fn: Callable,
    connect_account_fn: Callable,
    start_heartbeat_fn: Callable,
    strategy_classes=None,
    settings: Settings | None = None,
) -> None:
    ctx = build_app_fn(strategy_classes, settings)
    logger = get_logger("system")
    settings = ctx["settings"]
    conn_mgr = ctx["conn_mgr"]
    runner = ctx["runner"]
    watchdog = ctx["watchdog"]
    data_sub = ctx["data_sub"]

    stop_event = threading.Event()

    def _signal_handler(sig, frame):
        logger.info("收到退出信号(%s)，正在关闭...", sig)
        runner.stop()
        watchdog.stop()
        data_sub.stop()
        conn_mgr.disconnect()
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if not connect_account_fn(ctx, mode="live", stop_event=stop_event):
        conn_mgr.disconnect()
        return

    try:
        from web.backend.main import init_app_context, run_server
        from web.backend import routes as web_routes

        init_app_context(
            strategy_runner=runner,
            position_manager=ctx["pos_mgr"],
            order_manager=ctx["order_mgr"],
            data_manager=ctx["data_mgr"],
            connection_manager=conn_mgr,
            trade_executor=ctx["trade_exec"],
        )
        run_server(host=settings.WEB_HOST, port=settings.WEB_PORT)
        if getattr(web_routes, "_ws_manager", None):
            ctx["order_mgr"].set_trade_callback(web_routes._ws_manager.notify_trade_update)
    except Exception as exc:
        logger.warning("Web 服务未启动（可能缺少 fastapi/uvicorn）: %s", exc)

    watchdog.start()
    runner.start()
    watchdog.register_heartbeat("strategy_runner")

    data_thread = threading.Thread(target=data_sub.start, daemon=True, name="data-sub")
    data_thread.start()
    start_heartbeat_fn(ctx, stop_event, mode="live")

    logger.info("cytrade 运行中。按 Ctrl+C 退出。")
    stop_event.wait()
    logger.info("cytrade 已退出")


def run_managed_session(
    *,
    build_app_fn: Callable,
    connect_account_fn: Callable,
    start_heartbeat_fn: Callable,
    strategy_classes=None,
    settings: Settings | None = None,
    now_provider=None,
    sleep_fn=None,
) -> None:
    ctx = build_app_fn(strategy_classes, settings)
    logger = get_logger("system")
    settings = ctx["settings"]
    conn_mgr = ctx["conn_mgr"]
    runner = ctx["runner"]
    watchdog = ctx["watchdog"]
    data_sub = ctx["data_sub"]
    data_mgr = ctx["data_mgr"]
    now_provider = now_provider or datetime.now
    sleep_fn = sleep_fn or time.sleep

    stop_event = threading.Event()
    shutdown_lock = threading.Lock()
    data_thread = None

    def _shutdown(reason: str) -> None:
        with shutdown_lock:
            if stop_event.is_set():
                return
            logger.info("cytrade 正在关闭：%s", reason)
            stop_event.set()

        for label, action in (
            ("关闭策略运行器", runner.stop),
            ("关闭看门狗", watchdog.stop),
            ("停止行情订阅", data_sub.stop),
            ("断开交易连接", conn_mgr.disconnect),
            ("关闭数据管理器", data_mgr.close),
        ):
            try:
                action()
            except Exception as exc:
                logger.error("%s失败: %s", label, exc, exc_info=True)

    def _signal_handler(sig, frame):
        _shutdown(f"收到退出信号 {sig}")

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if not connect_account_fn(ctx, mode="managed", stop_event=stop_event):
        _shutdown("live 交易启动前置校验失败")
        return

    try:
        from web.backend.main import init_app_context, run_server
        from web.backend import routes as web_routes

        init_app_context(
            strategy_runner=runner,
            position_manager=ctx["pos_mgr"],
            order_manager=ctx["order_mgr"],
            data_manager=data_mgr,
            connection_manager=conn_mgr,
            trade_executor=ctx["trade_exec"],
        )
        run_server(host=settings.WEB_HOST, port=settings.WEB_PORT)
        if getattr(web_routes, "_ws_manager", None):
            ctx["order_mgr"].set_trade_callback(web_routes._ws_manager.notify_trade_update)
    except Exception as exc:
        logger.warning("Web 服务未启动（可能缺少 fastapi/uvicorn）: %s", exc)

    watchdog.start()
    runner.start()
    watchdog.register_heartbeat("strategy_runner")

    data_thread = threading.Thread(target=data_sub.start, daemon=True, name="data-sub")
    data_thread.start()
    start_heartbeat_fn(ctx, stop_event, mode="managed")

    def _session_guard() -> None:
        while not stop_event.is_set():
            now = now_provider()
            exit_at = build_session_time(now, settings.SESSION_EXIT_TIME)
            if now >= exit_at:
                _shutdown(f"已到收盘退出时间 {settings.SESSION_EXIT_TIME}")
                return
            remaining = max(0.0, (exit_at - now).total_seconds())
            sleep_fn(min(max(int(settings.SESSION_POLL_INTERVAL_SEC), 1), remaining))

    threading.Thread(target=_session_guard, daemon=True, name="session-guard").start()

    logger.info("cytrade 运行中，将在 %s 自动退出。", settings.SESSION_EXIT_TIME)
    stop_event.wait()

    if data_thread and data_thread.is_alive():
        data_thread.join(timeout=2)

    logger.info("cytrade 已退出")


def run_daily_session(
    *,
    run_managed_session_fn: Callable,
    strategy_classes=None,
    settings: Settings | None = None,
    now_provider=None,
    sleep_fn=None,
) -> str:
    settings = settings or Settings()
    now_provider = now_provider or datetime.now
    sleep_fn = sleep_fn or time.sleep
    logger = get_logger("system")
    now = now_provider()

    if not is_market_day(now):
        logger.info("今日非交易日，跳过会话启动并直接退出")
        return "skipped_non_trading_day"

    exit_at = build_session_time(now, settings.SESSION_EXIT_TIME)
    if now >= exit_at:
        logger.info("当前时间已超过收盘退出时间 %s，跳过本次会话", settings.SESSION_EXIT_TIME)
        return "skipped_after_close"

    stop_event = threading.Event()
    if not wait_until_session_start(settings, stop_event, now_provider, sleep_fn):
        logger.info("会话尚未启动即收到停止信号")
        return "stopped_before_start"

    run_managed_session_fn(
        strategy_classes=strategy_classes,
        settings=settings,
        now_provider=now_provider,
        sleep_fn=sleep_fn,
    )
    return "completed"


def run_daily_session_in_subprocess(
    strategy_specs: list[str],
    settings: Settings,
    *,
    run_daily_session_fn: Callable,
) -> str:
    strategy_classes = resolve_strategy_specs(strategy_specs)
    return run_daily_session_fn(strategy_classes=strategy_classes, settings=settings)


def launch_session_in_process(
    *,
    run_daily_session_in_subprocess_fn: Callable,
    strategy_classes=None,
    settings: Settings | None = None,
) -> str:
    settings = settings or Settings()
    strategy_specs = normalize_strategy_specs(strategy_classes)
    logger = get_logger("system")
    logger.info("父进程准备启动子进程交易会话")

    with ProcessPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_daily_session_in_subprocess_fn, strategy_specs, settings)
        result = future.result()

    logger.info("子进程交易会话结束: %s", result)
    return result


def run_scheduler_service(
    *,
    launch_session_fn: Callable,
    strategy_classes=None,
    settings: Settings | None = None,
    scheduler_cls=None,
) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    settings = settings or Settings()
    logger = get_logger("system")
    scheduler_cls = scheduler_cls or BlockingScheduler
    scheduler = scheduler_cls()
    start_hour, start_minute = parse_hhmm(settings.SESSION_START_TIME)
    strategy_specs = normalize_strategy_specs(strategy_classes)

    scheduler.add_job(
        launch_session_fn,
        trigger="cron",
        day_of_week="mon-fri",
        hour=start_hour,
        minute=start_minute,
        id="daily_trading_session",
        args=[strategy_specs, settings],
        max_instances=1,
        coalesce=True,
        misfire_grace_time=max(int(settings.SESSION_POLL_INTERVAL_SEC), 60),
    )

    def _signal_handler(sig, frame):
        logger.info("父进程收到退出信号(%s)，正在停止调度器", sig)
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info(
        "父进程调度器已启动：交易任务将在每个工作日 %s 触发，并在独立进程中运行",
        settings.SESSION_START_TIME,
    )
    scheduler.start()
