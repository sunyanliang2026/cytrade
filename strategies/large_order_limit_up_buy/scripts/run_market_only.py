"""Run the large-order limit-up buy strategy with the account disconnected."""
from __future__ import annotations

import argparse
import json
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
from main import _log_runtime_startup_config, _start_runtime_heartbeat, build_app
from monitor.logger import get_log_file_path, get_logger
from strategy.models import StrategyConfig
from strategies.large_order_limit_up_buy import LargeOrderLimitUpBuyStrategy


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
        for config in configs:
            runner.add_strategy(LargeOrderLimitUpBuyStrategy(config, ctx.get("trade_exec"), ctx.get("pos_mgr")))
        data_thread = threading.Thread(target=data_sub.start, daemon=True, name="large-order-data-sub")
        data_thread.start()
        _start_runtime_heartbeat(ctx, stop_event, mode="market-only")
        logger.info("LargeOrderLimitUpBuy running strategies=%d l2=%s", len(runner.get_all_strategies()), data_sub.get_l2_subscription_map())
        while not stop_event.is_set() and datetime.now() < stop_at:
            time.sleep(1)
    finally:
        runner.stop()
        data_sub.stop()
        logger.info(
            "LargeOrderLimitUpBuy stopped dry_run=true real_order_sent=false system_log=%s trade_log=%s",
            get_log_file_path("system"), get_log_file_path("trade"),
        )


if __name__ == "__main__":
    main()
