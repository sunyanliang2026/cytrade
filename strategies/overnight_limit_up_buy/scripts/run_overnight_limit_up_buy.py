"""Run the overnight limit-up buy strategy in dry-run mode.

The script waits until 08:30 by default, reads a strict stock_code/amount CSV,
gets today's limit-up price from xtdata, and submits mock BUY limit orders
through the shared TradeExecutor.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.trading_calendar import is_market_day
from config.settings import Settings
from monitor.logger import get_log_file_path, get_logger
from strategies.overnight_limit_up_buy.limit_price import LimitUpPriceProvider
from strategies.overnight_limit_up_buy.state import SubmissionStateStore
from strategies.overnight_limit_up_buy.strategy import OvernightLimitUpBuyStrategy, calculate_order_quantity
from strategy.models import StrategyConfig
from trading.executor import TradeExecutor
from trading.order_manager import OrderManager

SESSION_EVENT_PREFIX = "OVERNIGHT_LIMIT_UP_BUY_SESSION"
FAILED_RESULTS = {
    "skipped_missing_csv",
    "skipped_live_not_confirmed",
    "skipped_live_preflight_failed",
    "aborted_limit_up_price_unverified",
    "aborted_preflight_failed",
    "aborted_plan_not_confirmed",
}


@dataclass
class FrozenOrderPlan:
    config: StrategyConfig
    strategy: OvernightLimitUpBuyStrategy
    source_row: int
    request_key: str
    stock_code: str
    amount: float
    previous_close: float
    limit_up_price: float
    quantity: int
    estimated_amount: float


def default_csv_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "orders.csv"


def default_state_path() -> Path:
    return Path(__file__).resolve().parents[1] / "state" / "submitted_orders.json"


def parse_hhmmss(value: str) -> tuple[int, int, int]:
    parts = str(value or "").strip().split(":")
    if len(parts) == 2:
        parts.append("0")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"invalid time format: {value!r}, expected HH:MM[:SS]")
    try:
        hour, minute, second = (int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid time format: {value!r}, expected HH:MM[:SS]") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise argparse.ArgumentTypeError(f"invalid time range: {value!r}")
    return hour, minute, second


def build_submit_datetime(anchor: datetime, submit_time: str) -> datetime:
    hour, minute, second = parse_hhmmss(submit_time)
    return anchor.replace(hour=hour, minute=minute, second=second, microsecond=0)


def wait_until(target: datetime, logger, *, now_provider=None, sleep_fn=None) -> None:
    now_provider = now_provider or datetime.now
    sleep_fn = sleep_fn or time.sleep
    while True:
        now = now_provider()
        if now >= target:
            return
        remaining = max(0.0, (target - now).total_seconds())
        logger.info(
            "%s waiting target_time=%s remaining_sec=%.0f",
            SESSION_EVENT_PREFIX,
            target.strftime("%H:%M:%S"),
            remaining,
        )
        sleep_fn(min(30.0, max(0.1, remaining)))


def build_dry_run_executor() -> TradeExecutor:
    order_manager = OrderManager()
    return TradeExecutor(None, order_manager, live_trading_enabled=False)


def build_live_context(args: argparse.Namespace) -> dict | None:
    """Build and connect the shared runtime in explicit live mode."""

    logger = get_logger("system")
    from main import _connect_account_for_runtime, build_app

    settings = Settings(
        CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN=False,
        LOG_SUMMARY_MODE=not bool(args.full_console),
        LOAD_PREVIOUS_STATE_ON_START=False,
    )
    ctx = build_app(strategy_classes=[], settings=settings)
    if not _connect_account_for_runtime(ctx, mode="overnight_limit_up_buy_live"):
        logger.error("%s skipped reason=live_preflight_failed", SESSION_EVENT_PREFIX)
        safe_disconnect(ctx)
        return None
    return ctx


def safe_disconnect(ctx: dict | None) -> None:
    if not ctx:
        return
    conn_mgr = ctx.get("conn_mgr")
    if conn_mgr and hasattr(conn_mgr, "disconnect"):
        try:
            conn_mgr.disconnect()
        except Exception:
            get_logger("system").warning("%s live_disconnect_failed", SESSION_EVENT_PREFIX, exc_info=True)


def build_frozen_plan(configs, provider, executor, store, trade_day: date) -> tuple[list[FrozenOrderPlan], list[str]]:
    """Do all price, quantity, state, and account work before the target time."""

    plans: list[FrozenOrderPlan] = []
    failures: list[str] = []
    prices: dict[str, tuple[float, float]] = {}
    for config in configs:
        stock_code = str(config.stock_code or "")
        source_row = int((config.params or {}).get("source_row", 0) or 0)
        request_key = str((config.params or {}).get("request_key") or f"row:{source_row}")
        if store.was_submitted(trade_day.isoformat(), request_key):
            failures.append(f"row={source_row} stock={stock_code} reason=already_submitted_state_file")
            continue
        if stock_code not in prices:
            try:
                quote = provider.get_limit_up_quote(stock_code, expected_trade_day=trade_day)
                prices[stock_code] = (float(quote[0] or 0.0), float(quote[1] or 0.0))
            except AttributeError:
                try:
                    prices[stock_code] = (float(provider.get_limit_up_price(stock_code, expected_trade_day=trade_day) or 0.0), 0.0)
                except TypeError:
                    prices[stock_code] = (float(provider.get_limit_up_price(stock_code) or 0.0), 0.0)
            except TypeError:
                prices[stock_code] = (float(provider.get_limit_up_price(stock_code) or 0.0), 0.0)
        limit_up_price, previous_close = prices[stock_code]
        amount = float((config.params or {}).get("amount", 0.0) or 0.0)
        quantity = calculate_order_quantity(amount, limit_up_price)
        if limit_up_price <= 0:
            failures.append(f"row={source_row} stock={stock_code} reason=limit_up_price_unverified")
            continue
        if quantity <= 0:
            failures.append(f"row={source_row} stock={stock_code} reason=amount_less_than_one_lot")
            continue
        strategy = OvernightLimitUpBuyStrategy(config, executor)
        strategy.start()
        plans.append(FrozenOrderPlan(
            config=config,
            strategy=strategy,
            source_row=source_row,
            request_key=request_key,
            stock_code=stock_code,
            amount=amount,
            previous_close=previous_close,
            limit_up_price=limit_up_price,
            quantity=quantity,
            estimated_amount=limit_up_price * quantity,
        ))

    if failures or not plans:
        for plan in plans:
            plan.strategy.stop()
        return [], failures or ["reason=empty_frozen_plan"]

    arm_batch = getattr(executor, "arm_limit_buy_batch", None)
    if callable(arm_batch):
        funding = arm_batch([(plan.stock_code, plan.limit_up_price, plan.quantity) for plan in plans])
        if not bool(funding.get("ok")):
            for plan in plans:
                plan.strategy.stop()
            return [], [
                "reason=%s available_cash=%s required_amount=%.2f" % (
                    funding.get("reason", "batch_funding_preflight_failed"),
                    funding.get("available_cash", ""),
                    float(funding.get("required_amount", 0.0) or 0.0),
                )
            ]
        for plan in plans:
            plan.strategy.config.params["batch_available_cash"] = funding.get("available_cash")
    return plans, []


def display_frozen_plan(plans: list[FrozenOrderPlan], *, trade_day: date, submit_at: datetime) -> None:
    total = sum(plan.estimated_amount for plan in plans)
    available_cash = (plans[0].strategy.config.params or {}).get("batch_available_cash")
    print()
    print("Frozen overnight limit-up BUY plan")
    print(f"Trade day: {trade_day.isoformat()}  Submit time: {submit_at.strftime('%H:%M:%S')}")
    print("row  stock   amount       prev_close  limit_price  quantity  estimated_amount")
    for plan in plans:
        print(
            f"{plan.source_row:>3}  {plan.stock_code:<6}  {plan.amount:>11.2f}  "
            f"{plan.previous_close:>10.3f}  {plan.limit_up_price:>11.3f}  "
            f"{plan.quantity:>8}  {plan.estimated_amount:>16.2f}"
        )
    print(f"Total estimated amount: {total:.2f}")
    if available_cash is not None:
        print(f"Available cash at preflight: {float(available_cash):.2f}")
    print()


def confirm_frozen_plan(args: argparse.Namespace) -> bool:
    if not bool(getattr(args, "require_plan_confirm", False)):
        return True
    try:
        return input("Confirm frozen plan and wait for submission? Type 1 and press Enter: ").strip() == "1"
    except (EOFError, KeyboardInterrupt):
        return False


def run_session(
    args: argparse.Namespace,
    *,
    price_provider: LimitUpPriceProvider | None = None,
    trade_executor: TradeExecutor | None = None,
    now_provider=None,
    sleep_fn=None,
) -> str:
    logger = get_logger("system")
    csv_path = Path(args.csv).expanduser().resolve()
    state_path = Path(args.state_file).expanduser().resolve()
    now_provider = now_provider or datetime.now
    live_mode = bool(args.live)

    if not csv_path.is_file():
        logger.error("%s skipped reason=csv_missing csv=%s", SESSION_EVENT_PREFIX, csv_path)
        return "skipped_missing_csv"

    now = now_provider()
    if bool(args.market_day_only) and not is_market_day(now):
        logger.info("%s skipped reason=non_market_day date=%s", SESSION_EVENT_PREFIX, now.date().isoformat())
        return "skipped_non_market_day"

    submit_at = build_submit_datetime(now, str(args.submit_time))
    selector = OvernightLimitUpBuyStrategy(StrategyConfig(params={"csv_path": str(csv_path)}))
    configs = selector.select_stocks()
    if not configs:
        logger.warning("%s skipped reason=empty_csv csv=%s", SESSION_EVENT_PREFIX, csv_path)
        return "skipped_empty_csv"

    confirmation_error = validate_live_confirmation(args, configs) if live_mode else ""
    if confirmation_error:
        logger.error("%s skipped reason=%s live=true", SESSION_EVENT_PREFIX, confirmation_error)
        return "skipped_live_not_confirmed"

    live_ctx = None
    if live_mode and trade_executor is None:
        live_ctx = build_live_context(args)
        if live_ctx is None:
            return "skipped_live_preflight_failed"
        trade_executor = live_ctx["trade_exec"]

    logger.info(
        "%s preflight_start csv=%s state_file=%s submit_time=%s dry_run=%s live=%s system_log=%s trade_log=%s",
        SESSION_EVENT_PREFIX,
        csv_path,
        state_path,
        submit_at.strftime("%H:%M:%S"),
        not live_mode,
        live_mode,
        get_log_file_path("system"),
        get_log_file_path("trade"),
    )

    provider = price_provider or LimitUpPriceProvider()
    executor = trade_executor or build_dry_run_executor()
    store = SubmissionStateStore(state_path)
    trade_day = now.date()
    plans, failures = build_frozen_plan(configs, provider, executor, store, trade_day)
    if failures:
        for failure in failures:
            logger.error("%s preflight_failed %s", SESSION_EVENT_PREFIX, failure)
            print(f"PRE-FLIGHT FAILED: {failure}")
        if live_ctx is not None:
            safe_disconnect(live_ctx)
        return "aborted_preflight_failed"

    display_frozen_plan(plans, trade_day=trade_day, submit_at=submit_at)
    logger.info(
        "%s preflight_passed rows=%d total_estimated_amount=%.2f trade_day=%s",
        SESSION_EVENT_PREFIX,
        len(plans),
        sum(plan.estimated_amount for plan in plans),
        trade_day.isoformat(),
    )
    if not confirm_frozen_plan(args):
        logger.warning("%s aborted reason=frozen_plan_not_confirmed", SESSION_EVENT_PREFIX)
        for plan in plans:
            plan.strategy.stop()
        if live_ctx is not None:
            safe_disconnect(live_ctx)
        return "aborted_plan_not_confirmed"

    if not bool(args.no_wait):
        wait_until(submit_at, logger, now_provider=now_provider, sleep_fn=sleep_fn)

    logger.info("%s dispatch_start rows=%d", SESSION_EVENT_PREFIX, len(plans))

    results = []
    for plan in plans:
        result = plan.strategy.submit_once(
            trade_day=trade_day,
            price_lookup=lambda stock_code, price=plan.limit_up_price: price,
            submission_store=store,
            record_submission=False,
            log_submission=False,
        )
        results.append((plan, result))

    # All broker requests have been issued; persistence and detailed logging
    # are deliberately after the fast dispatch loop.
    for plan, result in results:
        plan.strategy.stop()
        if result.status == "submitted":
            store.record(
                trade_day.isoformat(),
                plan.request_key,
                {
                    "source_row": plan.source_row,
                    "stock_code": plan.stock_code,
                    "amount": plan.amount,
                    "limit_up_price": plan.limit_up_price,
                    "quantity": plan.quantity,
                    "order_uuid": result.order_uuid,
                },
            )
        logger.info(
            "%s result row=%d stock=%s status=%s seq=%d reason=%s amount=%.2f price=%.3f qty=%d order_uuid=%s",
            SESSION_EVENT_PREFIX, plan.source_row, result.stock_code, result.status, result.submission_seq,
            result.reason, result.amount, result.limit_up_price, result.quantity, result.order_uuid[:8],
        )

    submitted = sum(1 for _, item in results if item.status == "submitted")
    skipped = sum(1 for _, item in results if item.status == "skipped")
    rejected = sum(1 for _, item in results if item.status == "rejected")
    failed = sum(1 for _, item in results if item.status == "failed")
    logger.info(
        "%s stopped total=%d submitted=%d skipped=%d rejected=%d failed=%d dry_run=%s live=%s",
        SESSION_EVENT_PREFIX,
        len(results),
        submitted,
        skipped,
        rejected,
        failed,
        not live_mode,
        live_mode,
    )
    if live_ctx is not None:
        wait_sec = max(0.0, float(args.post_submit_wait_sec or 0.0))
        if wait_sec > 0:
            logger.info("%s post_submit_wait_sec=%.1f", SESSION_EVENT_PREFIX, wait_sec)
            time.sleep(wait_sec)
        safe_disconnect(live_ctx)
    return "completed" if submitted > 0 else "completed_no_submissions"


def validate_live_confirmation(args: argparse.Namespace, configs: list[StrategyConfig]) -> str:
    """Require an explicit acknowledgement before any live submission.

    The BAT displays and confirms the complete CSV before starting Python.
    The runner deliberately does not duplicate or compare the CSV contents.
    """

    if not bool(args.confirm_live):
        return "live_requires_confirm_live"
    return ""


def canonical_confirm_orders(configs: list[StrategyConfig]) -> str:
    parts = []
    for config in configs:
        amount = float((config.params or {}).get("amount", 0.0) or 0.0)
        parts.append(f"{str(config.stock_code or '').strip()}:{format_amount_for_confirmation(amount)}")
    return ";".join(parts)


def canonicalize_confirm_orders_text(value: str) -> str:
    parts = []
    for item in str(value or "").split(";"):
        text = item.strip()
        if not text:
            continue
        if ":" not in text:
            return str(value or "").strip()
        code, amount_text = text.split(":", 1)
        try:
            amount = float(amount_text)
        except ValueError:
            return str(value or "").strip()
        parts.append(f"{code.strip()}:{format_amount_for_confirmation(amount)}")
    return ";".join(parts)


def format_amount_for_confirmation(amount: float) -> str:
    value = float(amount or 0.0)
    if value.is_integer():
        return str(int(value))
    return f"{value:.10f}".rstrip("0").rstrip(".")


def normalize_time_text(value: str) -> str:
    hour, minute, second = parse_hhmmss(value)
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run overnight limit-up BUY orders in dry-run mode.")
    parser.add_argument("--csv", default=str(default_csv_path()), help="CSV with exactly stock_code,amount columns.")
    parser.add_argument("--state-file", default=str(default_state_path()), help="JSON file used for daily idempotency.")
    parser.add_argument("--submit-time", default="08:30:00", help="Submission time, HH:MM[:SS].")
    parser.add_argument("--no-wait", action="store_true", help="Submit immediately; useful for dry-run verification.")
    parser.add_argument("--market-day-only", dest="market_day_only", action="store_true", default=True)
    parser.add_argument("--no-market-day-only", dest="market_day_only", action="store_false")
    parser.add_argument("--live", action="store_true", help="Send a real order to the broker counter after strict confirmations.")
    parser.add_argument("--confirm-live", action="store_true", help="Required acknowledgement for --live.")
    parser.add_argument("--require-plan-confirm", action="store_true", help="Prompt after displaying the frozen plan before waiting.")
    parser.add_argument("--post-submit-wait-sec", type=float, default=10.0, help="Keep the live connection open briefly for async callbacks.")
    parser.add_argument("--full-console", action="store_true", help="Disable summary mode and print all console logs.")
    return parser


def main() -> None:
    result = run_session(build_parser().parse_args())
    if result in FAILED_RESULTS:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
