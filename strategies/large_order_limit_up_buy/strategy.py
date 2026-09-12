"""Level2 large-order limit-up buy strategy.

The strategy is intentionally independent from MainSealFollow. It observes
new limit-up buy orders, submits an immediate limit order in dry-run by
default, and records the observable queue context around that order.
"""
from __future__ import annotations

import csv
import json
import math
import time
from collections import deque
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any

from config.enums import OrderDirection, OrderStatus
from core.l2_models import L2OrderEvent, L2OrderQueueEvent, L2QuoteEvent, L2TransactionEvent
from core.models import TickData
from monitor.logger import get_logger
from strategy.base import BaseStrategy
from strategy.models import StrategyConfig
from strategies.overnight_limit_up_buy.limit_price import LimitUpPriceProvider

logger = get_logger("trade")


class LargeOrderLimitUpBuyStrategy(BaseStrategy):
    """Buy immediately after a qualifying new limit-up buy order appears."""

    strategy_name = "LargeOrderLimitUpBuy"
    max_positions = 200
    max_total_amount = 10_000_000.0

    DEFAULT_POOL = Path("strategies/large_order_limit_up_buy/data/manual_pool.csv")
    DEFAULT_RECORD_DIR = Path("strategies/large_order_limit_up_buy/data/runtime_records")
    NEIGHBOR_HEADERS = [
        "order_uuid", "code", "name", "relative_index", "entrust_no",
        "event_time", "price", "side", "volume", "lots", "amount",
        "is_big_order", "filled_volume", "cancelled_volume", "remaining_volume",
    ]
    SNAPSHOT_HEADERS = [
        "order_uuid", "xt_order_id", "code", "name", "submit_time", "price",
        "quantity", "lots", "front_order_count", "front_volume", "front_lots",
        "front_amount", "match_confidence", "trigger_entrust_no",
        "sealed_trade_amount", "recent_trade_amount", "recent_trade_rate",
    ]
    POST_ORDER_HEADERS = ["类型", "序号", "大单时间", "委托编号", "委托金额", "成交金额", "撤单金额", "剩余金额", "状态"]

    def __init__(self, config: StrategyConfig, trade_executor=None, position_manager=None):
        super().__init__(config, trade_executor, position_manager)
        params = dict(config.params or {})
        self._csv_path = Path(str(params.get("csv_path") or self.DEFAULT_POOL))
        self._stock_name = str(params.get("stock_name") or params.get("name") or "").strip()
        self._plan_amount = float(params.get("plan_amount", 0.0) or 0.0)
        self._dry_run = bool(params.get("dry_run", True))
        self._first_seal_big_order_min_amount = float(
            params.get("first_seal_big_order_min_amount", params.get("big_order_min_amount", 2_000_000.0))
            or 2_000_000.0
        )
        self._validation_big_order_min_amount = float(
            params.get("reseal_validation_big_order_min_amount", 1_500_000.0) or 1_500_000.0
        )
        self._reseal_validation_order_count = max(
            1, int(params.get("reseal_validation_order_count", 50) or 50)
        )
        self._reseal_validation_big_order_count = max(
            1, int(params.get("reseal_validation_big_order_count", 2) or 2)
        )
        self._reseal_min_prior_seal_amount = float(
            params.get("reseal_min_prior_seal_amount", 100_000_000.0) or 100_000_000.0
        )
        self._reseal_min_prior_seal_seconds = float(
            params.get("reseal_min_prior_seal_seconds", 20.0) or 20.0
        )
        self._reseal_min_reopen_seconds = float(
            params.get("reseal_min_reopen_seconds", 10.0) or 10.0
        )
        self._reseal_max_reopen_price_ratio = float(
            params.get("reseal_max_reopen_price_ratio", 0.985) or 0.985
        )
        self._neighbor_count = max(1, int(params.get("neighbor_count", 5) or 5))
        self._neighbor_window_seconds = max(0.0, float(params.get("neighbor_window_seconds", 3.0) or 3.0))
        self._record_dir = Path(str(params.get("record_dir") or self.DEFAULT_RECORD_DIR))
        self._limit_up_price = 0.0
        self._limit_up_price_warning_logged = False
        self._last_limit_up_lookup = 0.0
        self._initial_quote_checked = False
        self._entry_phase = "WAIT_INITIAL_QUOTE"
        self._sealed_since: datetime | None = None
        self._sealed_max_amount = 0.0
        self._last_seal_qualified = False
        self._reopen_since: datetime | None = None
        self._reopen_low_price = 0.0
        self._reseal_ready = False
        self._open_price = 0.0
        self._session_low_price = 0.0
        self._dip_requirement_logged = False
        self._dip_confirmed_logged = False
        self._big_order_count = 0
        self._decision_count = 0
        self._submitted_count = 0
        self._blocked_dip_count = 0
        self._blocked_active_count = 0
        self._blocked_position_count = 0
        self._pre_close = 0.0
        self._last_quote: L2QuoteEvent | None = None
        self._orders_by_no: dict[str, dict[str, Any]] = {}
        self._queue_orders: deque[dict[str, Any]] = deque(maxlen=2000)
        self._seen_entrust_nos: set[str] = set()
        self._active_order_uuid = ""
        self._active_trigger_entrust_no = ""
        self._reseal_validation_active = False
        self._reseal_validation_orders_seen = 0
        self._reseal_validation_big_orders_seen = 0
        self._reseal_validation_result = ""
        self._entry_filled = False
        self._trigger_count = 0
        self._sealed_trade_amount = 0.0
        self._recent_trades: deque[tuple[float, float]] = deque(maxlen=5000)
        self._last_queue: L2OrderQueueEvent | None = None
        self._raw_handle = None
        self._neighbor_handle = None
        self._snapshot_handle = None
        self._post_order_handle = None
        self._post_order_started_at: datetime | None = None
        self._post_order_big_orders: dict[str, dict[str, Any]] = {}
        self._post_order_sequence = 0
        self._post_order_written = False

    @classmethod
    def required_data_kinds(cls) -> set[str]:
        return {"tick", "l2quote", "l2order", "l2transaction", "l2orderqueue"}

    def current_data_kinds(self) -> set[str]:
        return self.required_data_kinds()

    def select_stocks(self) -> list[StrategyConfig]:
        path = self._csv_path
        if not path.is_file():
            raise RuntimeError(f"manual stock pool not found: {path}")
        configs: list[StrategyConfig] = []
        with path.open("r", encoding="utf-8-sig", newline="") as fp:
            for line_no, row in enumerate(csv.DictReader(fp), start=2):
                code = self._normalize_code(row.get("code") or row.get("stock_code") or row.get("股票代码"))
                if not code:
                    if any(str(value or "").strip() for value in row.values()):
                        logger.warning("%s invalid code line=%d", self.strategy_name, line_no)
                    continue
                name = str(row.get("name") or row.get("stock_name") or row.get("名称") or "").strip()
                amount = self._parse_number(row.get("plan_amount") or row.get("amount") or row.get("计划买入金额"))
                if amount <= 0:
                    logger.warning("%s skip code=%s reason=invalid_plan_amount", self.strategy_name, code)
                    continue
                configs.append(StrategyConfig(
                    stock_code=code,
                    max_position_amount=amount,
                    params={
                        "csv_path": str(path),
                        "stock_name": name,
                        "plan_amount": amount,
                        "dry_run": self._dry_run,
                        "first_seal_big_order_min_amount": self._first_seal_big_order_min_amount,
                        "reseal_validation_big_order_min_amount": self._validation_big_order_min_amount,
                        "reseal_validation_order_count": self._reseal_validation_order_count,
                        "reseal_validation_big_order_count": self._reseal_validation_big_order_count,
                        "reseal_min_prior_seal_amount": self._reseal_min_prior_seal_amount,
                        "reseal_min_prior_seal_seconds": self._reseal_min_prior_seal_seconds,
                        "reseal_min_reopen_seconds": self._reseal_min_reopen_seconds,
                        "reseal_max_reopen_price_ratio": self._reseal_max_reopen_price_ratio,
                        "neighbor_count": self._neighbor_count,
                        "neighbor_window_seconds": self._neighbor_window_seconds,
                        "record_dir": str(self._record_dir),
                    },
                ))
        return configs

    def start(self) -> None:
        super().start()
        self._open_record_files()

    def on_tick(self, tick: TickData) -> None:
        if tick.stock_code != self.stock_code:
            return
        event_time = tick.data_time or tick.recv_time
        if not self._is_continuous_trading_time(event_time):
            return
        open_price = float(tick.open or 0.0)
        low_price = float(tick.low or 0.0)
        if open_price > 0 and self._open_price <= 0:
            self._open_price = open_price
        if low_price <= 0:
            low_price = float(tick.last_price or 0.0)
        if low_price > 0:
            self._session_low_price = (
                low_price if self._session_low_price <= 0
                else min(self._session_low_price, low_price)
            )
        if self._entry_phase == "READY" and self._has_open_dip() and not self._dip_confirmed_logged:
            self._dip_confirmed_logged = True
            logger.info(
                "[LARGE_ORDER] %s opening_dip_confirmed open=%.3f low=%.3f ratio=%.4f",
                self.stock_code, self._open_price, self._session_low_price,
                self._session_low_price / self._open_price,
            )

    def on_l2_quote(self, event: L2QuoteEvent) -> None:
        if event.stock_code != self.stock_code:
            return
        self._last_quote = event
        self._pre_close = float(event.pre_close or self._pre_close or 0.0)
        if event.limit_up_price > 0:
            self._limit_up_price = float(event.limit_up_price)
        elif self._limit_up_price <= 0 and time.time() - self._last_limit_up_lookup >= 5.0:
            self._last_limit_up_lookup = time.time()
            exact_price = LimitUpPriceProvider().get_exact_limit_up_price(self.stock_code)
            if exact_price > 0:
                self._limit_up_price = exact_price
                self._log_event("limit_up_price_resolved", limit_up_price=exact_price, source="qmt_exact")
            elif not self._limit_up_price_warning_logged:
                self._limit_up_price_warning_logged = True
                self._log_event("limit_up_price_unavailable", reason="qmt_exact_price_missing")
        if self._limit_up_price <= 0:
            return
        quote_time = event.event_time or event.recv_time
        quote_is_sealed = self._quote_is_limit_up(event)
        if quote_is_sealed:
            if self._sealed_since is None:
                self._sealed_since = quote_time
                self._sealed_max_amount = 0.0
            self._sealed_max_amount = max(
                self._sealed_max_amount,
                self._quote_bid_amount(event),
            )
            if self._entry_phase == "WAIT_RESEAL" and self._reopen_since is not None:
                self._reseal_ready = self._reopen_conditions_met(quote_time)
                if self._reseal_ready:
                    self._log_event(
                        "reseal_conditions_met",
                        reopen_seconds=round(self._elapsed_seconds(self._reopen_since, quote_time), 3),
                        reopen_low_price=self._reopen_low_price,
                        required_low_price=self._limit_up_price * self._reseal_max_reopen_price_ratio,
                    )
        elif quote_time is not None:
            had_sealed_period = self._sealed_since is not None
            if self._sealed_since is not None:
                self._last_seal_qualified = self._seal_conditions_met(quote_time)
                self._log_event(
                    "seal_broken",
                    sealed_seconds=round(self._elapsed_seconds(self._sealed_since, quote_time), 3),
                    sealed_max_amount=round(self._sealed_max_amount, 2),
                    qualified=self._last_seal_qualified,
                )
            if self._sealed_since is not None and self._last_seal_qualified:
                self._entry_phase = "WAIT_RESEAL"
                self._reopen_since = quote_time
                self._reopen_low_price = self._quote_low_price(event)
                self._reseal_ready = False
                self._log_event("limit_up_reopened", limit_up_price=self._limit_up_price)
            elif self._entry_phase == "WAIT_RESEAL" and had_sealed_period:
                # A new seal must qualify on its own; never carry an older
                # reopen window across an intervening unqualified seal.
                self._entry_phase = "WAIT_REOPEN"
                self._reopen_since = None
                self._reopen_low_price = 0.0
                self._reseal_ready = False
                self._log_event("reseal_cycle_reset", reason="prior_seal_not_qualified")
            elif self._entry_phase == "WAIT_RESEAL" and self._reopen_since is not None:
                self._reopen_low_price = self._min_positive(
                    self._reopen_low_price, self._quote_low_price(event)
                )
            self._sealed_since = None
            self._sealed_max_amount = 0.0
        if not self._initial_quote_checked and not self._quote_has_price(event):
            return
        if not self._initial_quote_checked:
            self._initial_quote_checked = True
            if self._quote_is_limit_up(event):
                self._set_entry_phase("WAIT_REOPEN", "startup_already_limit_up")
                self._log_event("startup_already_limit_up", limit_up_price=self._limit_up_price)
            else:
                self._set_entry_phase("READY", "startup_not_limit_up")
        elif self._entry_phase == "WAIT_REOPEN" and self._quote_is_broken(event):
            # The transition is handled above only after a qualifying observed seal.
            if self._last_seal_qualified:
                self._set_entry_phase("WAIT_RESEAL", "limit_up_reopened")
        self._write_raw("l2quote", event.event_time, event.raw_xt_fields)

    def on_l2_order(self, event: L2OrderEvent) -> None:
        if event.stock_code != self.stock_code:
            return
        self._write_raw("l2order", event.event_time, event.raw_xt_fields)
        if not self._is_continuous_trading_time(event.event_time):
            return
        self._maybe_finish_post_order_window(event.event_time)
        if self._entry_phase == "WAIT_INITIAL_QUOTE" or self._entry_phase == "WAIT_REOPEN":
            return
        entrust_no = str(event.entrust_no or "").strip()
        if entrust_no and entrust_no in self._seen_entrust_nos:
            return
        if entrust_no:
            self._seen_entrust_nos.add(entrust_no)
        price = float(event.price or 0.0)
        side = self._normalize_side(event)
        if not self._is_limit_up_price(price) or side != "BUY" or bool(event.is_cancel):
            return
        volume = max(0, int(event.volume or 0))
        amount = float(event.amount or 0.0) or price * volume
        record = self._order_record(event, volume, amount, side)
        self._orders_by_no[entrust_no] = record if entrust_no else record
        self._queue_orders.append(record)
        post_window_active = self._post_order_started_at is not None
        if self._reseal_validation_active:
            self._observe_reseal_validation(record)
        elif self._entry_phase == "WAIT_RESEAL" and self._reseal_ready:
            # A reseal is time-sensitive: take the queue position first, then validate support.
            self._maybe_submit(record, reseal_validation=True)
        elif self._entry_phase == "READY" and self._is_first_seal_big_order(price, volume, amount):
            self._big_order_count += 1
            if not self._has_open_dip():
                self._blocked_dip_count += 1
                if not self._dip_requirement_logged:
                    self._dip_requirement_logged = True
                    self._log_event("buy_blocked", reason="opening_dip_not_confirmed", open_price=self._open_price,
                                    session_low_price=self._session_low_price, required_ratio=0.985)
            else:
                self._maybe_submit(record)
        self._write_pending_neighbors()
        if post_window_active:
            self._observe_post_order_record(record)

    def on_l2_transaction(self, event: L2TransactionEvent) -> None:
        if event.stock_code != self.stock_code:
            return
        self._write_raw("l2transaction", event.event_time, event.raw_xt_fields)
        amount = float(event.amount or 0.0) or float(event.price or 0.0) * int(event.volume or 0)
        if amount > 0 and float(event.price or 0.0) > 0:
            self._sealed_trade_amount += amount if self._is_limit_up_price(float(event.price)) else 0.0
            self._recent_trades.append((self._event_seconds(event.event_time), amount))
        if int(event.trade_flag or 0) == 3 or str(event.side or "").upper() == "CANCEL_BUY":
            ref = str(event.buy_no or "").strip()
            if ref in self._orders_by_no:
                self._orders_by_no[ref]["cancelled_volume"] = int(self._orders_by_no[ref].get("cancelled_volume", 0)) + int(event.volume or 0)
                self._orders_by_no[ref]["remaining_volume"] = max(
                    0, int(self._orders_by_no[ref]["volume"])
                    - int(self._orders_by_no[ref].get("filled_volume", 0))
                    - int(self._orders_by_no[ref].get("cancelled_volume", 0))
                )
                if ref == self._active_trigger_entrust_no:
                    self._log_event("trigger_order_canceled", trigger_entrust_no=ref)
                    self._active_trigger_entrust_no = ""
            self._update_post_order_record(ref)
        else:
            ref = str(event.buy_no or "").strip()
            if ref in self._orders_by_no:
                self._orders_by_no[ref]["filled_volume"] = int(self._orders_by_no[ref].get("filled_volume", 0)) + int(event.volume or 0)
                self._orders_by_no[ref]["remaining_volume"] = max(
                    0, int(self._orders_by_no[ref]["volume"]) - int(self._orders_by_no[ref]["filled_volume"])
                )
            self._update_post_order_record(ref)
        self._maybe_finish_post_order_window(event.event_time)

    def on_l2_orderqueue(self, event: L2OrderQueueEvent) -> None:
        if event.stock_code == self.stock_code:
            self._last_queue = event
            self._write_raw("l2orderqueue", event.event_time, event.raw_xt_fields)

    def _maybe_submit(self, trigger: dict[str, Any], *, reseal_validation: bool = False) -> None:
        if self._entry_filled:
            return
        if self._active_order_uuid:
            self._blocked_active_count += 1
            return
        if self._has_position():
            self._blocked_position_count += 1
            return
        price = float(trigger["price"])
        quantity = int(math.floor(self._plan_amount / price / 100.0) * 100)
        if quantity <= 0:
            self._log_event("buy_blocked", reason="plan_amount_below_one_lot", trigger=trigger)
            return
        front = [item for item in self._queue_orders if item is not trigger]
        front = front[-self._neighbor_count * 20:]
        now = time.time()
        front_volume = sum(int(item.get("volume", 0)) for item in front)
        front_amount = sum(float(item.get("amount", 0.0)) for item in front)
        front = front[-self._neighbor_count:]
        for index, item in enumerate(front, start=-len(front)):
            self._write_neighbor("queue_neighbor", trigger, item, index)
        self._trigger_count += 1
        self._decision_count += 1
        logger.info(
            "[LARGE_ORDER] %s BUY_DECISION price=%.3f volume=%d amount=%.2f trigger_entrust_no=%s",
            self.stock_code, price, int(trigger.get("volume", 0)), float(trigger.get("amount", 0.0)),
            trigger.get("entrust_no", ""),
        )
        remark = f"L2涨停大单打板 trigger={trigger.get('entrust_no', '')} front={front_volume}股"
        order = self.add_position(price, quantity, remark)
        if order is None:
            self._log_event("buy_blocked", reason="order_executor_unavailable", trigger=trigger)
            return
        self._active_order_uuid = str(order.order_uuid or "")
        self._submitted_count += 1
        self._active_trigger_entrust_no = str(trigger.get("entrust_no", "") or "")
        if reseal_validation:
            self._reseal_validation_active = True
            self._reseal_validation_orders_seen = 0
            self._reseal_validation_big_orders_seen = 0
            self._reseal_validation_result = "pending"
            self._log_event(
                "reseal_validation_started",
                order_uuid=self._active_order_uuid,
                required_orders=self._reseal_validation_order_count,
                required_big_orders=self._reseal_validation_big_order_count,
                big_order_min_amount=self._validation_big_order_min_amount,
            )
        self._start_post_order_window(trigger, reseal_validation=reseal_validation)
        self._write_snapshot(order, trigger, front_volume, front_amount, now)
        self._write_neighbor("our_order", trigger, {
            "entrust_no": self._active_order_uuid,
            "event_time": datetime.now(),
            "price": price,
            "side": "OUR_BUY",
            "volume": quantity,
            "amount": price * quantity,
            "is_big_order": False,
            "filled_volume": 0,
            "cancelled_volume": 0,
            "remaining_volume": quantity,
        }, 0)
        self._log_event("buy_submitted", trigger=trigger, front_volume=front_volume, front_amount=front_amount,
                        order_uuid=self._active_order_uuid, quantity=quantity, dry_run=self._dry_run)

    def _observe_reseal_validation(self, record: dict[str, Any]) -> None:
        """Evaluate only orders received after our reseal order was submitted."""
        self._reseal_validation_orders_seen += 1
        if bool(record.get("is_big_order")):
            self._reseal_validation_big_orders_seen += 1
            self._big_order_count += 1

        if self._reseal_validation_orders_seen < self._reseal_validation_order_count:
            return

        if self._reseal_validation_big_orders_seen >= self._reseal_validation_big_order_count:
            self._reseal_validation_active = False
            self._reseal_validation_result = "passed"
            self._log_event(
                "reseal_validation_passed",
                observed_orders=self._reseal_validation_orders_seen,
                observed_big_orders=self._reseal_validation_big_orders_seen,
                big_order_min_amount=self._validation_big_order_min_amount,
            )
            return

        self._reseal_validation_active = False
        self._reseal_validation_result = "failed"
        self._set_entry_phase("DONE", "reseal_validation_failed")
        cancel_order = getattr(self._trade_executor, "cancel_order", None)
        requested = bool(cancel_order(self._active_order_uuid, remark="reseal validation failed")) if callable(cancel_order) else False
        self._log_event(
            "reseal_validation_failed",
            observed_orders=self._reseal_validation_orders_seen,
            observed_big_orders=self._reseal_validation_big_orders_seen,
            required_big_orders=self._reseal_validation_big_order_count,
            cancel_requested=requested,
        )

    def _on_order_update_hook(self, order) -> None:
        if str(getattr(order, "order_uuid", "")) != self._active_order_uuid:
            return
        status = getattr(order, "status", None)
        if status in (OrderStatus.CANCELED, OrderStatus.PART_CANCEL, OrderStatus.JUNK, OrderStatus.UNKNOWN):
            self._active_order_uuid = ""
            self._log_event("our_order_finished", status=str(status), can_retrigger=True)
        elif status == OrderStatus.SUCCEEDED or (
            status == OrderStatus.PART_SUCC and int(getattr(order, "filled_quantity", 0) or 0) > 0
        ):
            if not self._entry_filled:
                self._entry_filled = True
                self._reseal_validation_active = False
                self._set_entry_phase("DONE", "entry_filled")
                self._log_event(
                    "our_order_filled",
                    status=str(status),
                    filled_quantity=int(getattr(order, "filled_quantity", 0) or 0),
                    can_retrigger=False,
                )

    def console_summary(self) -> str:
        return (
            f"{self.stock_code}:phase={self._entry_phase},open={self._open_price:.3f},"
            f"low={self._session_low_price:.3f},big={self._big_order_count},"
            f"decision={self._decision_count},submitted={self._submitted_count},"
            f"reseal_verify={self._reseal_validation_orders_seen}/{self._reseal_validation_order_count},"
            f"reseal_big={self._reseal_validation_big_orders_seen}/{self._reseal_validation_big_order_count},"
            f"reseal_result={self._reseal_validation_result or '-'},"
            f"entry_filled={self._entry_filled},"
            f"seal_max={self._sealed_max_amount:.2f},reopen_low={self._reopen_low_price:.3f},"
            f"reseal_ready={self._reseal_ready},"
            f"blocked_dip={self._blocked_dip_count},blocked_active={self._blocked_active_count},"
            f"blocked_position={self._blocked_position_count}"
        )

    def _open_record_files(self) -> None:
        day_dir = self._record_dir / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{self.stock_code}_{self.strategy_id[:8]}"
        raw_path = day_dir / f"{stem}.l2.jsonl"
        neighbor_path = day_dir / f"{stem}.queue_neighbors.csv"
        snapshot_path = day_dir / f"{stem}.queue_snapshots.csv"
        post_order_path = day_dir / f"{stem}.post_order_30s_orders.csv"
        self._raw_handle = raw_path.open("a", encoding="utf-8")
        self._neighbor_handle = neighbor_path.open("a", encoding="utf-8", newline="")
        self._snapshot_handle = snapshot_path.open("a", encoding="utf-8", newline="")
        self._post_order_handle = post_order_path.open("a", encoding="utf-8-sig", newline="")
        if neighbor_path.stat().st_size == 0:
            csv.writer(self._neighbor_handle).writerow(self.NEIGHBOR_HEADERS)
        if snapshot_path.stat().st_size == 0:
            csv.writer(self._snapshot_handle).writerow(self.SNAPSHOT_HEADERS)
        if post_order_path.stat().st_size == 0:
            csv.writer(self._post_order_handle).writerow(self.POST_ORDER_HEADERS)

    def stop(self) -> None:
        self._finish_post_order_window()
        for attr in ("_raw_handle", "_neighbor_handle", "_snapshot_handle", "_post_order_handle"):
            handle = getattr(self, attr, None)
            try:
                if handle:
                    handle.close()
            except Exception:
                pass
            setattr(self, attr, None)
        super().stop()

    def _write_raw(self, kind: str, event_time: datetime | None, payload: dict[str, Any]) -> None:
        if self._raw_handle is None:
            return
        self._raw_handle.write(json.dumps({"kind": kind, "code": self.stock_code, "name": self._stock_name,
                                            "event_time": self._format_time(event_time), "recv_time": datetime.now().isoformat(),
                                            "raw": payload or {}}, ensure_ascii=False, default=str) + "\n")
        self._raw_handle.flush()

    def _start_post_order_window(self, trigger: dict[str, Any], *, reseal_validation: bool) -> None:
        self._post_order_started_at = trigger.get("event_time") or datetime.now()
        self._post_order_trigger_type = "回封" if reseal_validation else "首封"
        self._post_order_big_orders.clear()
        self._post_order_sequence = 0
        self._post_order_written = False

    def _observe_post_order_record(self, record: dict[str, Any]) -> None:
        if self._post_order_started_at is None or self._post_order_written:
            return
        entrust_no = str(record.get("entrust_no", "") or "")
        if not entrust_no or not self._is_big_order(
            float(record.get("price", 0.0)), int(record.get("volume", 0)), float(record.get("amount", 0.0))
        ):
            return
        self._post_order_sequence += 1
        self._post_order_big_orders[entrust_no] = {
            "类型": self._post_order_trigger_type,
            "序号": self._post_order_sequence,
            "大单时间": self._format_time(record.get("event_time")),
            "委托编号": entrust_no,
            "委托金额": round(float(record.get("amount", 0.0)), 2),
            "成交金额": 0.0,
            "撤单金额": 0.0,
            "剩余金额": round(float(record.get("amount", 0.0)), 2),
            "状态": "未处理",
            "价格": float(record.get("price", 0.0)),
            "数量": int(record.get("volume", 0)),
        }

    def _update_post_order_record(self, entrust_no: str) -> None:
        item = self._post_order_big_orders.get(str(entrust_no or ""))
        source = self._orders_by_no.get(str(entrust_no or ""))
        if not item or not source:
            return
        price = float(item.get("价格", 0.0))
        filled = int(source.get("filled_volume", 0))
        cancelled = int(source.get("cancelled_volume", 0))
        item["成交金额"] = round(price * filled, 2)
        item["撤单金额"] = round(price * cancelled, 2)
        item["剩余金额"] = round(max(0, int(item["数量"]) - filled - cancelled) * price, 2)
        if item["剩余金额"] <= 0 and item["成交金额"] > 0 and item["撤单金额"] > 0:
            item["状态"] = "成交后撤单"
        elif item["撤单金额"] > 0:
            item["状态"] = "已撤单"
        elif item["成交金额"] >= item["委托金额"]:
            item["状态"] = "已成交"
        elif item["成交金额"] > 0:
            item["状态"] = "部分成交"
        else:
            item["状态"] = "未成交"

    def _maybe_finish_post_order_window(self, event_time: datetime | None) -> None:
        if self._post_order_started_at is None or event_time is None:
            return
        if self._elapsed_seconds(self._post_order_started_at, event_time) >= 30:
            self._finish_post_order_window()

    def _finish_post_order_window(self) -> None:
        if self._post_order_started_at is None or self._post_order_written:
            return
        for entrust_no in self._post_order_big_orders:
            self._update_post_order_record(entrust_no)
        if self._post_order_handle is not None:
            writer = csv.writer(self._post_order_handle)
            for item in self._post_order_big_orders.values():
                writer.writerow([item[key] for key in self.POST_ORDER_HEADERS])
            self._post_order_handle.flush()
        self._log_event(
            "post_order_30s_finished", trigger_type=self._post_order_trigger_type,
            big_orders=len(self._post_order_big_orders),
        )
        self._post_order_written = True
        self._post_order_started_at = None

    def _write_snapshot(self, order, trigger: dict[str, Any], front_volume: int, front_amount: float, now: float) -> None:
        if self._snapshot_handle is None:
            return
        rate = self._recent_trade_rate()
        csv.writer(self._snapshot_handle).writerow([
            order.order_uuid, getattr(order, "xt_order_id", 0), self.stock_code, self._stock_name,
            datetime.now().isoformat(), order.price, order.quantity, order.quantity // 100,
            len([x for x in self._queue_orders if x is not trigger]), front_volume, front_volume // 100,
            round(front_amount, 2), "event_order_sequence", trigger.get("entrust_no", ""),
            round(self._sealed_trade_amount, 2), round(sum(x[1] for x in self._recent_trades), 2), round(rate, 2),
        ])
        self._snapshot_handle.flush()

    def _write_pending_neighbors(self) -> None:
        if not self._active_order_uuid or not self._active_trigger_entrust_no:
            return
        trigger = self._orders_by_no.get(self._active_trigger_entrust_no)
        if not trigger:
            return
        after = list(self._queue_orders)[-self._neighbor_count:]
        for index, item in enumerate(after, start=1):
            if item is not trigger:
                self._write_neighbor("after_order", trigger, item, index)

    def _write_neighbor(self, kind: str, trigger: dict[str, Any], item: dict[str, Any], index: int) -> None:
        if self._neighbor_handle is None:
            return
        csv.writer(self._neighbor_handle).writerow([
            self._active_order_uuid, self.stock_code, self._stock_name, index,
            item.get("entrust_no", ""), self._format_time(item.get("event_time")), item.get("price", 0),
            item.get("side", ""), item.get("volume", 0), int(item.get("volume", 0)) // 100,
            round(float(item.get("amount", 0.0)), 2), int(bool(item.get("is_big_order"))),
            item.get("filled_volume", 0), item.get("cancelled_volume", 0), item.get("remaining_volume", item.get("volume", 0)),
        ])
        self._neighbor_handle.flush()

    def _log_event(self, event: str, **payload: Any) -> None:
        logger.info("%s %s", self.strategy_name, json.dumps({"event": event, "code": self.stock_code,
                                                               "name": self._stock_name, **payload}, ensure_ascii=False, default=str))

    def _is_big_order(self, price: float, volume: int, amount: float) -> bool:
        return amount >= self._validation_big_order_min_amount

    def _is_first_seal_big_order(self, price: float, volume: int, amount: float) -> bool:
        return amount >= self._first_seal_big_order_min_amount

    def _is_limit_up_price(self, price: float) -> bool:
        return self._limit_up_price > 0 and abs(price - self._limit_up_price) <= max(0.0001, self._limit_up_price * 0.00001)

    @staticmethod
    def _normalize_side(event: L2OrderEvent) -> str:
        side = str(event.side or "").strip().upper()
        if side:
            return side
        return {1: "BUY", 2: "SELL", 3: "CANCEL_BUY", 4: "CANCEL_SELL"}.get(int(event.entrust_direction or 0), "")

    def _order_record(self, event: L2OrderEvent, volume: int, amount: float, side: str) -> dict[str, Any]:
        return {"entrust_no": str(event.entrust_no or ""), "event_time": event.event_time,
                "price": float(event.price or 0.0), "side": side, "volume": volume, "amount": amount,
                "is_big_order": self._is_big_order(float(event.price or 0.0), volume, amount),
                "filled_volume": 0, "cancelled_volume": 0, "remaining_volume": volume}

    def _recent_trade_rate(self) -> float:
        cutoff = time.time() - 60.0
        amount = sum(value for timestamp, value in self._recent_trades if timestamp >= cutoff)
        return amount / 60.0

    @staticmethod
    def _event_seconds(value: datetime | None) -> float:
        return value.timestamp() if isinstance(value, datetime) else time.time()

    @staticmethod
    def _is_continuous_trading_time(value: datetime | None) -> bool:
        """Exclude all auction orders from the live trigger path."""
        return isinstance(value, datetime) and value.time() >= dt_time(9, 30)

    def _has_open_dip(self) -> bool:
        return self._open_price > 0 and self._session_low_price < self._open_price * 0.985

    def _quote_is_limit_up(self, event: L2QuoteEvent) -> bool:
        return self._is_limit_up_price(float(event.bid1 or 0.0))

    @staticmethod
    def _quote_has_price(event: L2QuoteEvent) -> bool:
        return float(event.bid1 or 0.0) > 0

    def _quote_is_broken(self, event: L2QuoteEvent) -> bool:
        return not self._is_limit_up_price(float(event.bid1 or 0.0))

    def _seal_conditions_met(self, quote_time: datetime | None) -> bool:
        return (
            self._sealed_since is not None
            and quote_time is not None
            and self._sealed_max_amount >= self._reseal_min_prior_seal_amount
            and self._elapsed_seconds(self._sealed_since, quote_time) > self._reseal_min_prior_seal_seconds
        )

    def _reopen_conditions_met(self, quote_time: datetime | None) -> bool:
        return (
            self._reopen_since is not None
            and quote_time is not None
            and self._elapsed_seconds(self._reopen_since, quote_time) >= self._reseal_min_reopen_seconds
            and self._reopen_low_price > 0
            and self._reopen_low_price < self._limit_up_price * self._reseal_max_reopen_price_ratio
        )

    @staticmethod
    def _elapsed_seconds(start: datetime | None, end: datetime | None) -> float:
        if start is None or end is None:
            return 0.0
        return max(0.0, (end - start).total_seconds())

    @staticmethod
    def _min_positive(current: float, value: float) -> float:
        if value <= 0:
            return current
        return value if current <= 0 else min(current, value)

    @staticmethod
    def _quote_bid_amount(event: L2QuoteEvent) -> float:
        # QMT l2quote bidVol is reported in lots; l2order volume is shares.
        return float(event.bid1 or 0.0) * max(0, int(event.bid1_volume or 0)) * 100

    @staticmethod
    def _quote_low_price(event: L2QuoteEvent) -> float:
        return float(event.last_price or 0.0) or float(event.bid1 or 0.0)

    def _set_entry_phase(self, phase: str, reason: str = "") -> None:
        if self._entry_phase == phase:
            return
        previous = self._entry_phase
        self._entry_phase = phase
        logger.info(
            "[LARGE_ORDER] %s phase=%s->%s%s",
            self.stock_code, previous, phase, f" reason={reason}" if reason else "",
        )

    @staticmethod
    def _format_time(value: Any) -> str:
        return value.isoformat() if isinstance(value, datetime) else str(value or "")

    @staticmethod
    def _normalize_code(value: Any) -> str:
        text = str(value or "").strip().upper()
        if "." in text:
            text = text.split(".", 1)[0]
        return text.zfill(6) if text.isdigit() and len(text) <= 6 else ""

    @staticmethod
    def _parse_number(value: Any) -> float:
        text = str(value or "").strip().replace(",", "")
        if not text:
            return 0.0
        multiplier = 10000.0 if "万" in text else 100000000.0 if "亿" in text else 1.0
        cleaned = "".join(char for char in text if char.isdigit() or char in ".-")
        try:
            return float(cleaned) * multiplier if cleaned else 0.0
        except ValueError:
            return 0.0

    def _has_position(self) -> bool:
        if not self._position_mgr:
            return False
        position = self._position_mgr.get_position(self.strategy_id)
        return bool(position and int(getattr(position, "total_quantity", 0) or 0) > 0)

    def __del__(self):
        for handle in (self._raw_handle, self._neighbor_handle, self._snapshot_handle):
            try:
                if handle:
                    handle.close()
            except Exception:
                pass
