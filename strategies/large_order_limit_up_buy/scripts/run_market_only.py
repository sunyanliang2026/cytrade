"""Run the large-order limit-up buy strategy with the account disconnected."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from datetime import datetime, time as dt_time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import Settings
from main import _log_runtime_startup_config, _start_runtime_heartbeat, build_app
from monitor.logger import get_log_file_path, get_logger
from strategy.models import StrategyConfig
from strategies.large_order_limit_up_buy import LargeOrderLimitUpBuyStrategy

SUMMARY_INTERVAL_SECONDS = 600


def _display_names(strategies) -> str:
    return "、".join(strategy.display_name() for strategy in strategies)


def _log_auction_failure(logger, strategies, reason: str, *, warning: bool = False) -> None:
    message = "[LARGE_ORDER] [竞价] 快照失败 %d只 原因=%s：%s；继续使用L2行情"
    args = (len(strategies), reason, _display_names(strategies))
    (logger.warning if warning else logger.info)(message, *args)


def _full_tick_first_value(payload: dict, field: str) -> float:
    value = payload.get(field)
    if value is None:
        return 0.0
    if not isinstance(value, (str, bytes)):
        try:
            value = value[0] if len(value) else 0.0
        except (TypeError, IndexError, KeyError):
            pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _full_tick_time(payload: dict, fallback: datetime) -> datetime:
    value = payload.get("time") or payload.get("sysTime")
    try:
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return datetime.fromtimestamp(numeric)
    except (TypeError, ValueError, OSError, OverflowError):
        return fallback


def _full_tick_limit_up(payload: dict) -> float:
    for field in ("upLimitPrice", "upperLimitPrice", "limitUp"):
        value = _full_tick_first_value(payload, field)
        if value > 0:
            return value
    return 0.0


def initialize_auction_states(strategies, logger, now: datetime | None = None) -> None:
    """Use the current ordinary tick during 09:25-09:30, with L2 fallback."""
    now = now or datetime.now()
    if now.time() < dt_time(9, 25):
        _log_auction_failure(logger, strategies, "尚未进入竞价窗口")
        return
    if now.time() >= dt_time(9, 30):
        _log_auction_failure(logger, strategies, "竞价窗口已结束")
        return

    try:
        from xtquant import xtdata
        xt_codes = [
            f"{strategy.stock_code}.SH" if strategy.stock_code.startswith("6") else f"{strategy.stock_code}.SZ"
            for strategy in strategies
        ]
        tick_map = xtdata.get_full_tick(xt_codes)
        if tick_map is None:
            tick_map = {}
    except Exception as exc:
        _log_auction_failure(logger, strategies, f"接口异常:{type(exc).__name__}", warning=True)
        return
    if not isinstance(tick_map, dict):
        _log_auction_failure(logger, strategies, "返回格式错误", warning=True)
        return

    normalized = {}
    for key, payload in tick_map.items():
        if isinstance(payload, dict):
            normalized[str(key).split(".", 1)[0]] = payload
    failures = {}
    for strategy in strategies:
        payload = normalized.get(strategy.stock_code)
        reason = ""
        if payload is None:
            reason = "stock_not_found"
        else:
            bid1 = _full_tick_first_value(payload, "bidPrice")
            if bid1 <= 0:
                reason = "invalid_bid1"
            elif not strategy.initialize_from_auction_tick(
                bid1=bid1,
                limit_up_price=_full_tick_limit_up(payload),
                event_time=_full_tick_time(payload, now),
            ):
                reason = "limit_up_price_unavailable"
        if reason:
            failures.setdefault(reason, []).append(strategy)
    reason_text = {
        "stock_not_found": "未返回该股票",
        "invalid_bid1": "买一价格无效",
        "limit_up_price_unavailable": "无法取得精确涨停价",
    }
    for reason, failed in failures.items():
        _log_auction_failure(logger, failed, reason_text.get(reason, reason), warning=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run LargeOrderLimitUpBuy in market-only dry-run mode.")
    parser.add_argument("--pool", default=str(LargeOrderLimitUpBuyStrategy.DEFAULT_POOL))
    parser.add_argument("--record-dir", default=str(LargeOrderLimitUpBuyStrategy.DEFAULT_RECORD_DIR))
    parser.add_argument("--stop-time", default="15:05")
    parser.add_argument("--neighbor-count", type=int, default=5)
    parser.add_argument("--neighbor-window-seconds", type=float, default=3.0)
    return parser


def load_strategy_config() -> dict:
    path = ROOT / "strategies" / "large_order_limit_up_buy" / "config" / "strategy_config.json"
    try:
        with path.open("r", encoding="utf-8") as fp:
            value = json.load(fp)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid strategy config {path}: {exc}") from exc


def session_time(now: datetime, value: str) -> datetime:
    hour, minute = (int(item) for item in value.split(":", 1))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def log_monitor_summary(logger, strategies, data_sub) -> None:
    status = data_sub.get_latest_data_status()
    latest = status.get("latest_data_time") or ""
    delay = float(status.get("data_delay_ms", 0.0) or 0.0)
    l2_map = data_sub.get_l2_subscription_map()
    submitted = sum(strategy._submitted_count for strategy in strategies)
    filled = sum(1 for strategy in strategies if strategy._entry_filled)
    canceled = sum(strategy._cancel_requested_count for strategy in strategies)
    logger.info(
        "[LARGE_ORDER] [汇总] 监控%d只 | 下单%d笔 | 成交%d笔 | 撤单%d笔 | L2%d只 | 延迟%.0f毫秒 | 行情%s",
        len(strategies), submitted, filled, canceled, len(l2_map), delay, latest,
    )
    groups = {}
    for strategy in strategies:
        groups.setdefault(strategy.console_status(), []).append(strategy)
    order = ["已成交", "验证失败已撤单", "等待首封", "等待开板", "等待回封", "等待首条行情", "当日结束"]
    for status in order:
        items = groups.get(status, [])
        if items:
            logger.info("[LARGE_ORDER] [%s] %d只：%s", status, len(items), _display_names(items))


def main() -> None:
    args = build_parser().parse_args()
    strategy_config = load_strategy_config()
    logger = get_logger("system")
    settings = Settings(
        LOAD_PREVIOUS_STATE_ON_START=False,
        CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN=True,
        LOG_SUMMARY_MODE=True,
        SESSION_EXIT_TIME=args.stop_time,
    )
    template = LargeOrderLimitUpBuyStrategy(
        StrategyConfig(params={
            "csv_path": args.pool,
            **strategy_config,
            "dry_run": True,
            "record_dir": args.record_dir,
            "neighbor_count": args.neighbor_count,
            "neighbor_window_seconds": args.neighbor_window_seconds,
        }),
        None,
        None,
    )
    configs = template.select_stocks()
    if not configs:
        raise SystemExit(f"manual stock pool is empty: {args.pool}")

    ctx = build_app(strategy_classes=[], settings=settings)
    runner = ctx["runner"]
    data_sub = ctx["data_sub"]
    stop_event = threading.Event()
    stop_at = session_time(datetime.now(), args.stop_time)

    def stop(sig=None, frame=None) -> None:
        logger.info("LargeOrderLimitUpBuy market-only stopping sig=%s", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    logger.info(
        "LargeOrderLimitUpBuy session_start dry_run=true pool=%s stocks=%d stop_at=%s account_connected=false",
        args.pool, len(configs), stop_at.strftime("%H:%M:%S"),
    )
    _log_runtime_startup_config(settings, ctx["conn_mgr"], mode="market-only")
    try:
        runner.start()
        strategies = []
        for config in configs:
            strategy = LargeOrderLimitUpBuyStrategy(config, ctx.get("trade_exec"), ctx.get("pos_mgr"))
            strategies.append(strategy)
            runner.add_strategy(strategy)
        initialize_auction_states(strategies, logger)
        data_thread = threading.Thread(target=data_sub.start, daemon=True, name="large-order-data-sub")
        data_thread.start()
        _start_runtime_heartbeat(ctx, stop_event, mode="market-only")
        logger.info("[LARGE_ORDER] monitoring_started live=false l2=%s", data_sub.get_l2_subscription_map())
        next_summary = time.monotonic() + SUMMARY_INTERVAL_SECONDS
        while not stop_event.is_set() and datetime.now() < stop_at:
            time.sleep(1)
            if time.monotonic() >= next_summary:
                log_monitor_summary(logger, strategies, data_sub)
                next_summary += SUMMARY_INTERVAL_SECONDS
    finally:
        runner.stop()
        data_sub.stop()
        logger.info(
            "LargeOrderLimitUpBuy stopped dry_run=true real_order_sent=false system_log=%s trade_log=%s",
            get_log_file_path("system"), get_log_file_path("trade"),
        )


if __name__ == "__main__":
    main()
