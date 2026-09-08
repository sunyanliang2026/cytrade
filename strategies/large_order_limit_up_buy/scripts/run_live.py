"""Explicit live runner for the large-order limit-up buy strategy.

This runner is intentionally separate from the dry-run entry. It uses the
manual CSV as the sole order plan and requires explicit acknowledgement before
the QMT account is connected and Level2 monitoring starts.
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import Settings
from core.trading_calendar import is_market_day
from main import _connect_account_for_runtime, _start_runtime_heartbeat, build_app
from monitor.logger import get_log_file_path, get_logger
from strategy.models import StrategyConfig
from strategies.large_order_limit_up_buy import LargeOrderLimitUpBuyStrategy
from strategies.large_order_limit_up_buy.scripts.run_market_only import load_strategy_config, session_time

SESSION_EVENT_PREFIX = "LARGE_ORDER_LIMIT_UP_BUY_LIVE"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run LargeOrderLimitUpBuy with explicit live confirmations.")
    parser.add_argument("--live", action="store_true", help="Required. Allows the runner to connect the trading account.")
    parser.add_argument("--confirm-live", action="store_true", help="Required acknowledgement for --live.")
    parser.add_argument("--pool", default=str(LargeOrderLimitUpBuyStrategy.DEFAULT_POOL))
    parser.add_argument("--record-dir", default=str(LargeOrderLimitUpBuyStrategy.DEFAULT_RECORD_DIR))
    parser.add_argument("--stop-time", default="15:05")
    parser.add_argument("--neighbor-count", type=int, default=5)
    parser.add_argument("--neighbor-window-seconds", type=float, default=3.0)
    parser.add_argument("--max-order-amount", type=float, default=0.0, help="Required hard limit for each CSV plan_amount.")
    parser.add_argument("--max-total-amount", type=float, default=0.0, help="Required hard limit for total CSV plan_amount.")
    parser.add_argument("--no-market-day-check", dest="market_day_only", action="store_false")
    parser.set_defaults(market_day_only=True)
    return parser


def format_amount(value: float) -> str:
    value = float(value or 0.0)
    return str(int(value)) if value.is_integer() else f"{value:.10f}".rstrip("0").rstrip(".")


def build_configs(args: argparse.Namespace) -> list[StrategyConfig]:
    strategy_config = load_strategy_config()
    template = LargeOrderLimitUpBuyStrategy(
        StrategyConfig(params={
            "csv_path": args.pool,
            **strategy_config,
            "dry_run": False,
            "record_dir": args.record_dir,
            "neighbor_count": args.neighbor_count,
            "neighbor_window_seconds": args.neighbor_window_seconds,
        }),
        None,
        None,
    )
    return template.select_stocks()


def validate_live_confirmation(args: argparse.Namespace, configs: list[StrategyConfig]) -> str:
    if not bool(args.live):
        return "live_requires_live_flag"
    if not bool(args.confirm_live):
        return "live_requires_confirm_live"
    if not configs:
        return "live_requires_nonempty_pool"
    if float(args.max_order_amount or 0.0) <= 0:
        return "live_requires_max_order_amount"
    if float(args.max_total_amount or 0.0) <= 0:
        return "live_requires_max_total_amount"

    planned_amounts = [float((item.params or {}).get("plan_amount", 0.0) or 0.0) for item in configs]
    if any(amount > float(args.max_order_amount) for amount in planned_amounts):
        return "live_plan_exceeds_max_order_amount"
    if sum(planned_amounts) > float(args.max_total_amount):
        return "live_plan_exceeds_max_total_amount"
    return ""


def run_live_session(args: argparse.Namespace) -> str:
    logger = get_logger("system")
    configs = build_configs(args)
    if not configs:
        logger.error("%s skipped reason=empty_pool pool=%s", SESSION_EVENT_PREFIX, args.pool)
        return "skipped_empty_pool"
    confirmation_error = validate_live_confirmation(args, configs)
    if confirmation_error:
        logger.error("%s skipped reason=%s", SESSION_EVENT_PREFIX, confirmation_error)
        return "skipped_live_not_confirmed"
    if bool(args.market_day_only) and not is_market_day(datetime.now()):
        logger.info("%s skipped reason=non_market_day", SESSION_EVENT_PREFIX)
        return "skipped_non_market_day"

    settings = Settings(
        LOAD_PREVIOUS_STATE_ON_START=False,
        CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN=False,
        LOG_SUMMARY_MODE=True,
        SESSION_EXIT_TIME=args.stop_time,
    )
    ctx = build_app(strategy_classes=[], settings=settings)
    stop_event = threading.Event()
    if not _connect_account_for_runtime(ctx, mode="large_order_limit_up_buy_live", stop_event=stop_event):
        logger.error("%s skipped reason=live_preflight_failed", SESSION_EVENT_PREFIX)
        connection = ctx.get("conn_mgr")
        if connection and hasattr(connection, "disconnect"):
            connection.disconnect()
        return "skipped_live_preflight_failed"

    runner = ctx["runner"]
    data_sub = ctx["data_sub"]
    stop_at = session_time(datetime.now(), args.stop_time)

    def stop(sig=None, frame=None) -> None:
        logger.info("%s stopping sig=%s", SESSION_EVENT_PREFIX, sig)
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    logger.warning(
        "%s start live=true dry_run=false pool=%s code=%s amount=%s max_order_amount=%s max_total_amount=%s stop_at=%s",
        SESSION_EVENT_PREFIX,
        args.pool,
        ",".join(item.stock_code for item in configs),
        format_amount(sum(float((item.params or {}).get("plan_amount", 0.0) or 0.0) for item in configs)),
        format_amount(args.max_order_amount),
        format_amount(args.max_total_amount),
        stop_at.strftime("%H:%M:%S"),
    )
    try:
        runner.start()
        for config in configs:
            runner.add_strategy(LargeOrderLimitUpBuyStrategy(config, ctx["trade_exec"], ctx["pos_mgr"]))
        data_thread = threading.Thread(target=data_sub.start, daemon=True, name="large-order-live-data-sub")
        data_thread.start()
        _start_runtime_heartbeat(ctx, stop_event, mode="live")
        logger.info("%s monitoring_started l2=%s", SESSION_EVENT_PREFIX, data_sub.get_l2_subscription_map())
        while not stop_event.is_set() and datetime.now() < stop_at:
            time.sleep(1)
    finally:
        runner.stop()
        data_sub.stop()
        connection = ctx.get("conn_mgr")
        if connection and hasattr(connection, "disconnect"):
            connection.disconnect()
        logger.info(
            "%s stopped live=true system_log=%s trade_log=%s",
            SESSION_EVENT_PREFIX, get_log_file_path("system"), get_log_file_path("trade"),
        )
    return "completed"


def main() -> None:
    result = run_live_session(build_parser().parse_args())
    if result.startswith("skipped_"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
