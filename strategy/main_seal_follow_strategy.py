"""Level2-driven main-seal follow strategy skeleton.

This module intentionally focuses on framework integration first:
- CSV stock-pool loading
- strategy instance creation
- state persistence / restore
- split-entry order submission and callback binding
- Level2 event caching

The actual "main seal" entry and cancellation rules are migrated in the
next iteration after the infrastructure is stable.
"""
from __future__ import annotations

import csv
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional

from config.enums import OrderDirection, OrderStatus
from config.settings import settings as global_settings
from core.l2_models import L2OrderEvent, L2OrderQueueEvent, L2QuoteEvent, L2TransactionEvent
from core.models import TickData
from monitor.logger import get_logger
from strategy.base import BaseStrategy
from strategy.models import StrategyConfig
from trading.models import Order

logger = get_logger("trade")


class MainSealFollowStrategy(BaseStrategy):
    """Main-seal follow strategy skeleton driven by Level2 data."""

    strategy_name = "MainSealFollow"
    max_positions = 200
    max_total_amount = 10_000_000.0

    CSV_CODE_KEYS = (
        "code",
        "stock_code",
        "\u8bc1\u5238\u4ee3\u7801",
        "\u80a1\u7968\u4ee3\u7801",
    )
    CSV_NAME_KEYS = (
        "name",
        "stock_name",
        "\u8bc1\u5238\u540d\u79f0",
        "\u80a1\u7968\u540d\u79f0",
        "\u540d\u79f0",
    )
    CSV_AMOUNT_KEYS = (
        "plan_amount",
        "amount",
        "\u8ba1\u5212\u4e70\u5165\u91d1\u989d",
        "\u4e70\u5165\u91d1\u989d",
    )

    STATE_WAIT_SIGNAL = "WAIT_SIGNAL"
    STATE_WAIT_ORDER_ACK = "WAIT_ORDER_ACK"
    STATE_IN_QUEUE = "IN_QUEUE"
    STATE_HAS_POSITION = "HAS_POSITION"
    STATE_EXITED = "EXITED"
    STATE_DRY_RUN_READY = "DRY_RUN_READY"

    def __init__(self, config: StrategyConfig, trade_executor=None, position_manager=None):
        super().__init__(config, trade_executor, position_manager)
        params = self.config.params or {}

        self._csv_path = str(params.get("csv_path") or self._default_csv_path())
        self._stock_name = str(params.get("stock_name") or params.get("name") or "")
        self._plan_amount = float(params.get("plan_amount", 0.0) or 0.0)
        self._dry_run = bool(params.get("dry_run", True))
        self._big_amount_min = float(params.get("big_amount_min", 2_000_000.0) or 2_000_000.0)
        self._queue_vol_unit = str(params.get("queue_vol_unit") or "share").strip().lower()
        self._order_vol_unit = str(params.get("order_vol_unit") or "share").strip().lower()
        self._trade_vol_unit = str(params.get("trade_vol_unit") or "share").strip().lower()
        self._sweep_window_ms = int(params.get("sweep_window_ms", 1_200) or 1_200)
        self._sweep_near_limit_ticks = int(params.get("sweep_near_limit_ticks", 3) or 3)
        self._sweep_min_amount = float(params.get("sweep_min_amount", 5_000_000.0) or 5_000_000.0)
        self._main_seal_window_ms = int(params.get("main_seal_window_ms", 1_000) or 1_000)
        self._require_recent_big_limit_buy = bool(params.get("require_recent_big_limit_buy", True))
        self._block_on_recent_big_limit_cancel = bool(params.get("block_on_recent_big_limit_cancel", True))
        self._main_seal_front_max_index = int(params.get("main_seal_front_max_index", 5) or 5)
        self._front_big_weak_ratio = float(params.get("front_big_weak_ratio", 0.50) or 0.50)
        self._back_big_min_amount = float(params.get("back_big_min_amount", 2_000_000.0) or 2_000_000.0)
        self._max_queue_ms = int(params.get("max_queue_ms", 8_000) or 8_000)
        self._cooldown_ms = int(params.get("cooldown_ms", 5_000) or 5_000)

        self._entry_state = self.STATE_WAIT_SIGNAL
        self._limit_up_price = float(params.get("limit_up_price", 0.0) or 0.0)
        self._last_price = float(params.get("last_price", 0.0) or 0.0)
        self._target_lots = int(params.get("target_lots", 0) or 0)
        self._target_shares = int(params.get("target_shares", 0) or 0)
        self._feature_split_lots: List[int] = list(params.get("feature_split_lots", []) or [])
        self._entry_order_uuids: List[str] = list(params.get("entry_order_uuids", []) or [])
        self._current_queue: List[int] = list(params.get("current_queue", []) or [])
        self._current_queue_time_ms = int(params.get("current_queue_time_ms", 0) or 0)
        self._queue_before_send: List[int] = list(params.get("queue_before_send", []) or [])
        self._front_qty_anchor = int(params.get("front_qty_anchor", 0) or 0)
        self._my_start = int(params.get("my_start", 0) or 0)
        self._my_end = int(params.get("my_end", 0) or 0)
        self._entry_queue: List[int] = list(params.get("entry_queue", []) or [])
        self._entry_position: Dict[str, object] = dict(params.get("entry_position", {}) or {})
        self._position_method = str(params.get("position_method") or "")
        self._position_confidence = str(params.get("position_confidence") or "")
        self._queue_enter_time_ms = int(params.get("queue_enter_time_ms", 0) or 0)
        self._last_cancel_time_ms = int(params.get("last_cancel_time_ms", 0) or 0)
        self._last_cancel_reason = str(params.get("last_cancel_reason") or "")

        self._recent_trades: Deque[dict] = deque(maxlen=500)
        self._recent_big_limit_buy_orders: Deque[dict] = deque(maxlen=200)
        self._recent_big_limit_cancel_orders: Deque[dict] = deque(maxlen=200)

    @classmethod
    def required_data_kinds(cls) -> set[str]:
        return {"tick", "l2quote", "l2transaction", "l2order", "l2orderqueue"}

    def select_stocks(self) -> List[StrategyConfig]:
        raw_path = str(self._csv_path or self._default_csv_path() or "").strip()
        if not raw_path:
            logger.warning("MainSealFollow: csv_path is empty, skip selection")
            return []

        csv_path = Path(raw_path)
        if not csv_path.is_file():
            logger.warning("MainSealFollow: CSV file not found, skip selection: %s", csv_path)
            return []

        configs: List[StrategyConfig] = []
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fp:
            reader = csv.DictReader(fp)
            for row_no, row in enumerate(reader, start=2):
                try:
                    stock_code = self._normalize_stock_code(self._get_first_present(row, self.CSV_CODE_KEYS))
                    stock_name = str(self._get_first_present(row, self.CSV_NAME_KEYS, "")).strip()
                    plan_amount = self._parse_amount(self._get_first_present(row, self.CSV_AMOUNT_KEYS, 0))
                    if not stock_code or plan_amount <= 0:
                        continue

                    configs.append(
                        StrategyConfig(
                            stock_code=stock_code,
                            max_position_amount=plan_amount,
                            params={
                                "csv_path": str(csv_path),
                                "stock_name": stock_name,
                                "plan_amount": plan_amount,
                                "instance_key": stock_code,
                                "dry_run": self._dry_run,
                                "big_amount_min": self._big_amount_min,
                                "queue_vol_unit": self._queue_vol_unit,
                                "order_vol_unit": self._order_vol_unit,
                                "trade_vol_unit": self._trade_vol_unit,
                                "sweep_window_ms": self._sweep_window_ms,
                                "sweep_near_limit_ticks": self._sweep_near_limit_ticks,
                                "sweep_min_amount": self._sweep_min_amount,
                                "main_seal_window_ms": self._main_seal_window_ms,
                                "require_recent_big_limit_buy": self._require_recent_big_limit_buy,
                                "block_on_recent_big_limit_cancel": self._block_on_recent_big_limit_cancel,
                                "main_seal_front_max_index": self._main_seal_front_max_index,
                                "front_big_weak_ratio": self._front_big_weak_ratio,
                                "back_big_min_amount": self._back_big_min_amount,
                                "max_queue_ms": self._max_queue_ms,
                                "cooldown_ms": self._cooldown_ms,
                                "source_row": row_no,
                            },
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "MainSealFollow: failed to parse CSV row %d, skip. row=%s err=%s",
                        row_no,
                        row,
                        exc,
                    )
        return configs

    def on_tick(self, tick: TickData) -> Optional[dict]:
        self._last_price = float(getattr(tick, "last_price", 0.0) or 0.0)
        return None

    def prepare_for_trading_day(self, trade_day: str) -> bool:
        self._limit_up_price = 0.0
        self._last_price = 0.0
        self._target_lots = 0
        self._target_shares = 0
        self._feature_split_lots = []
        self._current_queue = []
        self._current_queue_time_ms = 0
        self._queue_before_send = []
        self._front_qty_anchor = 0
        self._my_start = 0
        self._my_end = 0
        self._entry_queue = []
        self._entry_position = {}
        self._position_method = ""
        self._position_confidence = ""
        self._queue_enter_time_ms = 0
        self._last_cancel_time_ms = 0
        self._last_cancel_reason = ""
        self._recent_trades.clear()
        self._recent_big_limit_buy_orders.clear()
        self._recent_big_limit_cancel_orders.clear()

        if self._has_position():
            self._entry_state = self.STATE_HAS_POSITION
        elif self._has_active_entry_order():
            self._entry_state = self.STATE_WAIT_ORDER_ACK
        elif self._entry_state != self.STATE_DRY_RUN_READY:
            self._entry_state = self.STATE_WAIT_SIGNAL
        return True

    def on_l2_quote(self, event: L2QuoteEvent) -> None:
        self._last_price = float(event.last_price or self._last_price or 0.0)
        if float(event.limit_up_price or 0.0) > 0:
            self._limit_up_price = float(event.limit_up_price)
        elif float(event.pre_close or 0.0) > 0:
            self._limit_up_price = self._calc_limit_up(self.stock_code, float(event.pre_close))

        if self._plan_amount > 0 and self._limit_up_price > 0 and self._target_lots <= 0:
            self._refresh_target_from_limit_price(self._limit_up_price)
        self._maybe_trigger_entry("l2quote")

    def on_l2_transaction(self, event: L2TransactionEvent) -> None:
        shares = self._trade_vol_to_shares(event.volume)
        amount = float(event.amount or self._amount_of(float(event.price or 0.0), shares))
        self._recent_trades.append(
            {
                "time": self._to_ms(event.event_time),
                "price": float(event.price or 0.0),
                "volume": shares,
                "amount": amount,
                "side": str(event.side or ""),
            }
        )
        self._maybe_trigger_entry("l2transaction")

    def on_l2_order(self, event: L2OrderEvent) -> None:
        if self._limit_up_price <= 0:
            return
        shares = self._order_vol_to_shares(event.volume)
        amount = float(event.amount or self._amount_of(float(event.price or 0.0), shares))
        if amount < self._big_amount_min:
            return
        if not self._price_eq(float(event.price or 0.0), self._limit_up_price):
            return
        if not self._is_buy_side(str(event.side or "")):
            return

        payload = {
            "time": self._to_ms(event.event_time),
            "price": float(event.price or 0.0),
            "volume": shares,
            "amount": amount,
            "entrust_no": str(event.entrust_no or ""),
        }
        if event.is_cancel:
            self._recent_big_limit_cancel_orders.append(payload)
        else:
            self._recent_big_limit_buy_orders.append(payload)
        self._maybe_trigger_entry("l2order")

    def on_l2_orderqueue(self, event: L2OrderQueueEvent) -> None:
        queue_price = float(event.price or 0.0)
        self._current_queue_time_ms = self._to_ms(event.event_time)
        if self._limit_up_price > 0 and queue_price > 0 and not self._price_eq(queue_price, self._limit_up_price):
            self._current_queue = []
            if self._has_reported_entry_order() and self._entry_state != self.STATE_HAS_POSITION:
                self._request_cancel_entry_orders("bid_level_price_not_limit_fallback")
            return
        self._current_queue = self._queue_to_shares_list(event.bid_level_volume or [])
        if self._has_active_entry_order() and self._entry_state != self.STATE_HAS_POSITION:
            if self._has_reported_entry_order():
                if not self._entry_position:
                    if self._on_queue_for_order_accepted():
                        self._analyze_queue_and_maybe_cancel()
                else:
                    self._analyze_queue_and_maybe_cancel()
            else:
                self._entry_state = self.STATE_WAIT_ORDER_ACK
        self._maybe_trigger_entry("l2orderqueue")

    def submit_feature_entry_orders(self, limit_price: float, trigger_reason: str = "") -> List[Order]:
        if self._has_position():
            logger.info("MainSealFollow[%s]: already has position, skip entry", self.strategy_id[:8])
            return []
        if self._has_active_entry_order():
            logger.info("MainSealFollow[%s]: active entry orders exist, skip duplicate submit", self.strategy_id[:8])
            return []
        if not self._dry_run and not self._trade_executor:
            logger.warning(
                "MainSealFollow[%s]: trade executor is not configured, skip entry submit",
                self.strategy_id[:8],
            )
            return []
        if limit_price <= 0 or self._plan_amount <= 0:
            logger.warning(
                "MainSealFollow[%s]: invalid submit params limit_price=%s plan_amount=%s",
                self.strategy_id[:8],
                limit_price,
                self._plan_amount,
            )
            return []

        self._refresh_target_from_limit_price(limit_price)
        if self._target_lots <= 0 or self._target_shares <= 0:
            logger.warning(
                "MainSealFollow[%s]: planned amount is insufficient for one lot at limit price %.3f",
                self.strategy_id[:8],
                limit_price,
            )
            return []

        planned_parts = self._plan_entry_parts(limit_price, trigger_reason=trigger_reason)
        if not planned_parts:
            logger.warning(
                "MainSealFollow[%s]: no valid split orders generated at limit price %.3f",
                self.strategy_id[:8],
                limit_price,
            )
            return []

        if self._dry_run:
            self._entry_state = self.STATE_DRY_RUN_READY
            logger.info(
                "MainSealFollow[%s] [DRY_RUN]: ready to submit %d split orders stock=%s limit=%.3f shares=%d parts=%s reason=%s",
                self.strategy_id[:8],
                len(planned_parts),
                self.stock_code,
                limit_price,
                self._target_shares,
                [item["quantity"] for item in planned_parts],
                trigger_reason or "manual",
            )
            return []

        orders: List[Order] = []
        self._queue_before_send = list(self._current_queue)
        self._front_qty_anchor = sum(self._queue_before_send)
        for item in planned_parts:
            order = self._trade_executor.buy_limit(
                self.strategy_id,
                self.strategy_name,
                self.stock_code,
                limit_price,
                int(item["quantity"]),
                str(item["remark"]),
            )
            if not order:
                continue
            self._track_order(order)
            self._entry_order_uuids.append(order.order_uuid)
            orders.append(order)

        if orders:
            self._entry_state = self.STATE_WAIT_ORDER_ACK
            self.request_state_persist(reason=f"msf_submit:{self.strategy_id}")
        return orders

    def recover_unfilled_entry_state(self) -> None:
        super().recover_unfilled_entry_state()
        if not self._has_position() and not self._has_active_entry_order():
            if self._has_any_recorded_entry_fill():
                self._entry_state = self.STATE_HAS_POSITION
                return
            self._entry_state = self.STATE_WAIT_SIGNAL
            self._entry_order_uuids = []
            self._queue_before_send = []
            self._front_qty_anchor = 0
            self._my_start = 0
            self._my_end = 0
            self._entry_queue = []
            self._entry_position = {}
            self._position_method = ""
            self._position_confidence = ""
            self._queue_enter_time_ms = 0

    def persistent_instance_fields(self) -> List[str]:
        return [
            "_csv_path",
            "_stock_name",
            "_plan_amount",
            "_dry_run",
            "_big_amount_min",
            "_queue_vol_unit",
            "_order_vol_unit",
            "_trade_vol_unit",
            "_sweep_window_ms",
            "_sweep_near_limit_ticks",
            "_sweep_min_amount",
            "_main_seal_window_ms",
            "_require_recent_big_limit_buy",
            "_block_on_recent_big_limit_cancel",
            "_main_seal_front_max_index",
            "_front_big_weak_ratio",
            "_back_big_min_amount",
            "_max_queue_ms",
            "_cooldown_ms",
            "_entry_state",
            "_limit_up_price",
            "_last_price",
            "_target_lots",
            "_target_shares",
            "_feature_split_lots",
            "_entry_order_uuids",
            "_current_queue",
            "_current_queue_time_ms",
            "_queue_before_send",
            "_front_qty_anchor",
            "_my_start",
            "_my_end",
            "_entry_queue",
            "_entry_position",
            "_position_method",
            "_position_confidence",
            "_queue_enter_time_ms",
            "_last_cancel_time_ms",
            "_last_cancel_reason",
        ]

    def _get_custom_state(self) -> dict:
        state = self._export_state_fields(self.persistent_instance_fields())
        state["recent_trades"] = list(self._recent_trades)
        state["recent_big_limit_buy_orders"] = list(self._recent_big_limit_buy_orders)
        state["recent_big_limit_cancel_orders"] = list(self._recent_big_limit_cancel_orders)
        return state

    def _restore_custom_state(self, state: dict) -> None:
        self._restore_state_fields(state, self.persistent_instance_fields())
        self._recent_trades = deque(list(state.get("recent_trades", []) or []), maxlen=500)
        self._recent_big_limit_buy_orders = deque(
            list(state.get("recent_big_limit_buy_orders", []) or []),
            maxlen=200,
        )
        self._recent_big_limit_cancel_orders = deque(
            list(state.get("recent_big_limit_cancel_orders", []) or []),
            maxlen=200,
        )

    def _on_order_update_hook(self, order: Order) -> None:
        if order.direction != OrderDirection.BUY:
            if order.direction == OrderDirection.SELL and not self._has_position():
                self._entry_state = self.STATE_EXITED
            return

        if order.order_uuid not in self._entry_order_uuids:
            self._entry_order_uuids.append(order.order_uuid)

        if self._order_has_any_fill(order):
            self._entry_state = self.STATE_HAS_POSITION
            self._cancel_remaining_entry_orders("deal_happened_cancel_remaining")
            return

        if self._has_position() or self._has_any_recorded_entry_fill():
            self._entry_state = self.STATE_HAS_POSITION
            return

        if order.status in (
            OrderStatus.UNREPORTED,
            OrderStatus.WAIT_REPORTING,
            OrderStatus.REPORTED,
        ):
            self._entry_state = self.STATE_WAIT_ORDER_ACK
            if self._current_queue and self._has_reported_entry_order():
                if not self._entry_position:
                    if self._on_queue_for_order_accepted():
                        self._analyze_queue_and_maybe_cancel()
                else:
                    self._analyze_queue_and_maybe_cancel()
            return

        if order.status in (
            OrderStatus.REPORTED_CANCEL,
            OrderStatus.PARTSUCC_CANCEL,
            OrderStatus.PART_SUCC,
        ):
            self._entry_state = self.STATE_IN_QUEUE
            if (
                self._current_queue
                and not self._has_pending_cancel_entry_order()
                and self._has_reported_entry_order()
            ):
                if not self._entry_position:
                    if self._on_queue_for_order_accepted():
                        self._analyze_queue_and_maybe_cancel()
                else:
                    self._analyze_queue_and_maybe_cancel()
            return

        if order.status in (
            OrderStatus.CANCELED,
            OrderStatus.PART_CANCEL,
            OrderStatus.JUNK,
            OrderStatus.UNKNOWN,
        ) and not self._has_active_entry_order():
            self._entry_state = self.STATE_WAIT_SIGNAL

    def _refresh_target_from_limit_price(self, limit_price: float) -> None:
        self._target_lots = self._calc_target_lots(self._plan_amount, limit_price)
        self._target_shares = self._target_lots * 100
        self._feature_split_lots = self._make_feature_split_lots(self._target_lots)

    def _plan_entry_parts(self, limit_price: float, trigger_reason: str = "") -> List[Dict[str, object]]:
        if not self._feature_split_lots:
            self._refresh_target_from_limit_price(limit_price)
        parts: List[Dict[str, object]] = []
        total_parts = len(self._feature_split_lots)
        for index, lots in enumerate(self._feature_split_lots, start=1):
            quantity = int(lots) * 100
            if quantity <= 0:
                continue
            parts.append(
                {
                    "quantity": quantity,
                    "remark": (
                        f"MSF entry part={index}/{total_parts} "
                        f"limit={limit_price:.3f} trigger={trigger_reason or 'manual'}"
                    ),
                }
            )
        return parts

    def _maybe_trigger_entry(self, trigger_source: str) -> bool:
        if self._entry_state != self.STATE_WAIT_SIGNAL:
            return False
        if self._has_position() or self._has_active_entry_order():
            return False
        if self._limit_up_price <= 0 or not self._current_queue:
            return False
        if self._last_cancel_time_ms > 0 and self._now_ms() - self._last_cancel_time_ms < self._cooldown_ms:
            return False
        if not self._is_sweep_ok():
            return False
        if not self._main_seal_ok():
            return False
        orders = self.submit_feature_entry_orders(
            self._limit_up_price,
            trigger_reason=f"l2:{trigger_source}",
        )
        return bool(orders) or self._entry_state == self.STATE_DRY_RUN_READY

    def _on_queue_for_order_accepted(self) -> bool:
        if not self._current_queue or not self._has_reported_entry_order():
            return False
        start, end, method, confidence = self._estimate_my_region(self._current_queue)
        self._my_start = int(start)
        self._my_end = int(end)
        self._position_method = str(method)
        self._position_confidence = str(confidence)
        self._entry_queue = list(self._current_queue)
        self._entry_position = self._analyze_position(self._current_queue)
        self._queue_enter_time_ms = self._current_queue_time_ms or self._now_ms()
        self._entry_state = self.STATE_IN_QUEUE
        self.request_state_persist(reason=f"msf_queue_estimated:{self.strategy_id}", min_interval_sec=0.0)
        return True

    def _analyze_queue_and_maybe_cancel(self) -> str:
        if (
            not self._current_queue
            or self._limit_up_price <= 0
            or not self._entry_position
            or self._has_pending_cancel_entry_order()
        ):
            return ""

        position = self._analyze_position(self._current_queue)
        entry_front_big_amount = float(self._entry_position.get("front_big_amount", 0.0) or 0.0)
        entry_front_big_count = int(self._entry_position.get("front_big_count", 0) or 0)

        front_big_weak = False
        if entry_front_big_amount > 0:
            front_big_weak = position["front_big_amount"] < entry_front_big_amount * self._front_big_weak_ratio
        if entry_front_big_count > 0 and int(position["front_big_count"]) == 0:
            front_big_weak = True

        back_big_weak = position["back_big_amount"] < self._back_big_min_amount
        position_danger = position["front_big_amount"] < self._big_amount_min
        elapsed = max(0, self._now_ms() - int(self._queue_enter_time_ms or 0))
        timeout_weak = elapsed > self._max_queue_ms and back_big_weak

        if front_big_weak and back_big_weak and self._position_method == "pattern_contiguous":
            self._request_cancel_entry_orders("front_big_weak_and_back_big_empty")
            return "front_big_weak_and_back_big_empty"
        if position_danger and back_big_weak:
            self._request_cancel_entry_orders("position_danger_and_back_big_empty")
            return "position_danger_and_back_big_empty"
        if front_big_weak and back_big_weak:
            self._request_cancel_entry_orders("front_big_weak_and_back_big_empty")
            return "front_big_weak_and_back_big_empty"
        if timeout_weak:
            self._request_cancel_entry_orders("queue_timeout_and_back_big_empty")
            return "queue_timeout_and_back_big_empty"
        return ""

    def _request_cancel_entry_orders(self, reason: str) -> int:
        cancel_order = getattr(self._trade_executor, "cancel_order", None)
        if not callable(cancel_order):
            return 0
        if (
            reason
            and self._last_cancel_reason == reason
            and self._last_cancel_time_ms > 0
            and self._now_ms() - self._last_cancel_time_ms < 1_000
        ):
            return 0

        submitted = 0
        for order in self._get_active_entry_orders():
            if order.status in (OrderStatus.REPORTED_CANCEL, OrderStatus.PARTSUCC_CANCEL):
                continue
            if bool(cancel_order(order.order_uuid, remark=f"MSF cancel: {reason}")):
                submitted += 1

        if submitted > 0:
            self._last_cancel_time_ms = self._now_ms()
            self._last_cancel_reason = str(reason or "")
            self.request_state_persist(reason=f"msf_cancel:{self.strategy_id}", min_interval_sec=0.0)
        return submitted

    def _cancel_remaining_entry_orders(self, reason: str) -> int:
        if not self._get_active_entry_orders():
            return 0
        return self._request_cancel_entry_orders(reason)

    def _get_active_entry_orders(self) -> List[Order]:
        return [
            order
            for order in list(self._pending_orders.values())
            if order.direction == OrderDirection.BUY and order.is_active()
        ]

    def _has_reported_entry_order(self) -> bool:
        report_statuses = {
            OrderStatus.REPORTED,
            OrderStatus.REPORTED_CANCEL,
            OrderStatus.PARTSUCC_CANCEL,
            OrderStatus.PART_SUCC,
            OrderStatus.SUCCEEDED,
        }
        return any(order.status in report_statuses for order in self._get_active_entry_orders()) or any(
            self._order_has_any_fill(order) for order in self._get_active_entry_orders()
        )

    def _has_pending_cancel_entry_order(self) -> bool:
        return any(
            order.status in (OrderStatus.REPORTED_CANCEL, OrderStatus.PARTSUCC_CANCEL)
            for order in self._get_active_entry_orders()
        )

    def _has_any_recorded_entry_fill(self) -> bool:
        entry_order_ids = set(self._entry_order_uuids)
        if not entry_order_ids:
            return False

        seen: set[str] = set()
        for order in list(self._pending_orders.values()) + list(getattr(self, "_orders_history", []) or []):
            order_uuid = str(getattr(order, "order_uuid", "") or "")
            if not order_uuid or order_uuid in seen or order_uuid not in entry_order_ids:
                continue
            seen.add(order_uuid)
            if self._order_has_any_fill(order):
                return True
        return False

    @staticmethod
    def _order_has_any_fill(order: Order) -> bool:
        return int(getattr(order, "filled_quantity", 0) or 0) > 0

    def _analyze_position(self, queue_shares: List[int]) -> Dict[str, object]:
        size = len(queue_shares or [])
        my_start = max(0, min(int(self._my_start or 0), size))
        my_end = max(my_start - 1, min(int(self._my_end or 0), size - 1))

        front = list(queue_shares[:my_start])
        self_part = list(queue_shares[my_start:my_end + 1])
        back = list(queue_shares[my_end + 1:])

        front_big = self._calc_big_metrics(front, self._limit_up_price)
        back_big = self._calc_big_metrics(back, self._limit_up_price)

        front_total = sum(front)
        back_total = sum(back)
        self_total = sum(self_part)
        total = front_total + back_total + self_total

        return {
            "front_total": front_total,
            "back_total": back_total,
            "self_total": self_total,
            "total": total,
            "my_position_ratio_all": float(front_total) / total if total > 0 else 0.0,
            "front_big_count": int(front_big["big_count"]),
            "front_big_amount": float(front_big["big_amount"]),
            "back_big_count": int(back_big["big_count"]),
            "back_big_amount": float(back_big["big_amount"]),
        }

    @staticmethod
    def _lots_to_shares(pattern_lots: List[int]) -> List[int]:
        return [int(value or 0) * 100 for value in list(pattern_lots or []) if int(value or 0) > 0]

    @staticmethod
    def _find_pattern_region(after_queue: List[int], pattern_shares: List[int]):
        queue_size = len(after_queue or [])
        pattern_size = len(pattern_shares or [])
        if queue_size < pattern_size or pattern_size <= 0:
            return None

        for start in range(0, queue_size - pattern_size + 1):
            matched = True
            for offset in range(pattern_size):
                if int(after_queue[start + offset]) != int(pattern_shares[offset]):
                    matched = False
                    break
            if matched:
                return start, start + pattern_size - 1, "pattern_contiguous"
        return None

    def _estimate_region_by_front_anchor(self, after_queue: List[int]):
        accumulated = 0
        for index, value in enumerate(after_queue or []):
            accumulated += int(value or 0)
            if accumulated >= int(self._front_qty_anchor or 0):
                start = min(index + 1, len(after_queue))
                end = start
                return start, end, "front_qty_anchor"
        return len(after_queue), len(after_queue), "tail_estimated"

    def _estimate_my_region(self, after_queue: List[int]):
        pattern = self._lots_to_shares(self._feature_split_lots)
        found = self._find_pattern_region(after_queue, pattern)
        if found is not None:
            start, end, method = found
            return start, end, method, "HIGH"

        start, end, method = self._estimate_region_by_front_anchor(after_queue)
        return start, end, method, "LOW"

    def _has_position(self) -> bool:
        if not self._position_mgr:
            return False
        position = self._position_mgr.get_position(self.strategy_id)
        return bool(position and int(getattr(position, "total_quantity", 0) or 0) > 0)

    @staticmethod
    def _default_csv_path() -> str:
        return str(getattr(global_settings, "CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH", "") or "")

    @staticmethod
    def _get_first_present(row: dict, keys, default=""):
        for key in keys:
            if key in row and row[key] not in (None, ""):
                return row[key]
        return default

    @staticmethod
    def _parse_amount(value) -> float:
        text = (
            str(value or "")
            .strip()
            .replace(",", "")
            .replace("\uFF0C", "")
        )
        if not text:
            return 0.0
        if text.endswith("\u4e07"):
            return float(text[:-1]) * 10000.0
        return float(text)

    @staticmethod
    def _normalize_stock_code(value: str) -> str:
        text = str(value or "").strip().upper().replace("/", "").replace(" ", "")
        if not text:
            return ""
        if "." in text:
            text = text.split(".", 1)[0]
        if text.startswith("SH") and len(text) >= 8:
            return text[2:].zfill(6)
        if text.startswith("SZ") and len(text) >= 8:
            return text[2:].zfill(6)
        if text.startswith("BJ") and len(text) >= 8:
            return text[2:].zfill(6)
        return text.zfill(6)

    @staticmethod
    def _calc_limit_up(stock_code: str, pre_close: float) -> float:
        code = str(stock_code or "").strip().zfill(6)
        if pre_close <= 0:
            return 0.0
        if code.startswith(("300", "301", "688")):
            return round(pre_close * 1.20 + 1e-8, 2)
        if code.startswith(("8", "4", "9")):
            return round(pre_close * 1.30 + 1e-8, 2)
        return round(pre_close * 1.10 + 1e-8, 2)

    @staticmethod
    def _calc_target_lots(plan_amount: float, limit_price: float) -> int:
        if plan_amount <= 0 or limit_price <= 0:
            return 0
        lots = int(float(plan_amount) / (float(limit_price) * 100.0))
        return lots if lots >= 1 else 0

    @staticmethod
    def _make_feature_split_lots(total_lots: int) -> List[int]:
        total_lots = int(total_lots or 0)
        if total_lots <= 0:
            return []
        if total_lots == 1:
            return [1]
        if total_lots <= 5:
            return [total_lots - 1, 1]
        if total_lots <= 10:
            return [total_lots - 2, 2]
        if total_lots <= 20:
            return [total_lots - 3, 3]
        if total_lots <= 50:
            return [total_lots - 7, 7]
        return [total_lots - 13, 13]

    @staticmethod
    def _is_buy_side(value: str) -> bool:
        text = str(value or "").strip().upper()
        return text in {"B", "BUY", "23", "1"} or "BUY" in text

    def _queue_to_shares_list(self, raw_queue) -> List[int]:
        out: List[int] = []
        for value in list(raw_queue or []):
            shares = self._normalize_volume_to_shares(value, self._queue_vol_unit)
            if shares > 0:
                out.append(shares)
        return out

    def _trade_vol_to_shares(self, volume) -> int:
        return self._normalize_volume_to_shares(volume, self._trade_vol_unit)

    def _order_vol_to_shares(self, volume) -> int:
        return self._normalize_volume_to_shares(volume, self._order_vol_unit)

    @staticmethod
    def _normalize_volume_to_shares(volume, unit: str) -> int:
        try:
            normalized = int(float(volume or 0))
        except (TypeError, ValueError):
            return 0
        if str(unit or "").strip().lower() == "lot":
            return normalized * 100
        return normalized

    @staticmethod
    def _amount_of(price: float, shares: int) -> float:
        return float(price or 0.0) * int(shares or 0)

    def _recent_items(self, items: Deque[dict], window_ms: int) -> List[dict]:
        current = self._now_ms()
        return [
            item
            for item in list(items)
            if current - int(item.get("time", current) or current) <= int(window_ms or 0)
        ]

    def _big_orders_from_queue(self, queue_shares: List[int], price: float) -> List[tuple[int, int, float]]:
        out: List[tuple[int, int, float]] = []
        for index, shares in enumerate(queue_shares or []):
            amount = self._amount_of(price, shares)
            if amount >= self._big_amount_min:
                out.append((index, shares, amount))
        return out

    def _calc_big_metrics(self, queue_shares: List[int], price: float) -> dict:
        bigs = self._big_orders_from_queue(queue_shares, price)
        return {
            "big_count": len(bigs),
            "big_amount": sum(item[2] for item in bigs),
            "first_big_index": bigs[0][0] if bigs else None,
        }

    def _detect_main_seal_from_queue(self, queue_shares: List[int], price: float) -> bool:
        metrics = self._calc_big_metrics(queue_shares, price)
        first_big_index = metrics.get("first_big_index")
        if metrics.get("big_count", 0) <= 0 or first_big_index is None:
            return False
        return int(first_big_index) <= self._main_seal_front_max_index

    def _recent_big_limit_buy_ok(self) -> bool:
        if self._limit_up_price <= 0:
            return False
        return bool(self._recent_items(self._recent_big_limit_buy_orders, self._main_seal_window_ms))

    def _recent_big_limit_cancel_blocked(self) -> bool:
        if self._limit_up_price <= 0:
            return False
        recent_cancels = self._recent_items(self._recent_big_limit_cancel_orders, self._main_seal_window_ms)
        if not recent_cancels:
            return False
        recent_buys = self._recent_items(self._recent_big_limit_buy_orders, self._main_seal_window_ms)
        if not recent_buys:
            return True
        latest_buy_time = max(int(item.get("time", 0) or 0) for item in recent_buys)
        return any(int(item.get("time", 0) or 0) >= latest_buy_time for item in recent_cancels)

    def _main_seal_ok(self) -> bool:
        if self._limit_up_price <= 0 or not self._current_queue:
            return False
        if not self._detect_main_seal_from_queue(self._current_queue, self._limit_up_price):
            return False
        if self._require_recent_big_limit_buy and not self._recent_big_limit_buy_ok():
            return False
        if self._block_on_recent_big_limit_cancel and self._recent_big_limit_cancel_blocked():
            return False
        return True

    def _is_sweep_ok(self) -> bool:
        if self._limit_up_price <= 0:
            return False

        near_price = self._limit_up_price - float(self._sweep_near_limit_ticks or 0) * 0.01
        near_trades = [
            item
            for item in self._recent_items(self._recent_trades, self._sweep_window_ms)
            if float(item.get("price", 0.0) or 0.0) >= near_price
        ]
        if not near_trades:
            return False

        total_amount = 0.0
        hit_limit = False
        for item in near_trades:
            price = float(item.get("price", 0.0) or 0.0)
            shares = int(item.get("volume", 0) or 0)
            amount = float(item.get("amount", 0.0) or self._amount_of(price, shares))
            total_amount += amount
            if self._price_eq(price, self._limit_up_price):
                hit_limit = True
        return hit_limit and total_amount >= self._sweep_min_amount

    @staticmethod
    def _price_eq(a: float, b: float, tick: float = 0.01) -> bool:
        if a is None or b is None:
            return False
        return abs(float(a) - float(b)) < tick / 2.0

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _to_ms(value) -> int:
        if value is None:
            return 0
        if hasattr(value, "timestamp"):
            try:
                return int(float(value.timestamp()) * 1000)
            except Exception:
                return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0


__all__ = ["MainSealFollowStrategy"]
