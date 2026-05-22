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
import json
import os
import threading
import time
from collections import deque
from datetime import datetime
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
    STATE_CHAIN_SUBMITTED = "CHAIN_SUBMITTED"
    STATE_WAIT_PROBE_FILL = "WAIT_PROBE_FILL"
    STATE_PROBE_FILLED_DECISION = "PROBE_FILLED_DECISION"
    STATE_MAIN_KEEPING = "MAIN_KEEPING"
    STATE_MAIN_CANCELING = "MAIN_CANCELING"
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
        self._dry_run = bool(
            params.get(
                "dry_run",
                getattr(global_settings, "CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN", True),
            )
        )
        self._dry_run_replay_probe_logic = bool(params.get("dry_run_replay_probe_logic", True))
        self._big_amount_min = float(params.get("big_amount_min", 2_000_000.0) or 2_000_000.0)
        self._queue_vol_unit = str(params.get("queue_vol_unit") or "lot").strip().lower()
        self._order_vol_unit = str(params.get("order_vol_unit") or "share").strip().lower()
        self._trade_vol_unit = str(params.get("trade_vol_unit") or "share").strip().lower()
        self._sweep_window_ms = int(params.get("sweep_window_ms", 1_200) or 1_200)
        self._sweep_near_limit_ticks = int(params.get("sweep_near_limit_ticks", 3) or 3)
        self._sweep_min_amount = float(params.get("sweep_min_amount", 5_000_000.0) or 5_000_000.0)
        self._quote_trigger_near_limit_ticks = int(
            params.get("quote_trigger_near_limit_ticks", max(self._sweep_near_limit_ticks, 5)) or max(self._sweep_near_limit_ticks, 5)
        )
        self._main_seal_window_ms = int(params.get("main_seal_window_ms", 1_000) or 1_000)
        self._require_recent_big_limit_buy = bool(params.get("require_recent_big_limit_buy", True))
        self._block_on_recent_big_limit_cancel = bool(params.get("block_on_recent_big_limit_cancel", True))
        self._allow_existing_limit_queue = bool(params.get("allow_existing_limit_queue", False))
        self._existing_limit_observe_ms = int(params.get("existing_limit_observe_ms", 3_000))
        self._existing_limit_min_bid1_lot = int(params.get("existing_limit_min_bid1_lot", 0) or 0)
        self._existing_limit_min_reported_orders = int(params.get("existing_limit_min_reported_orders", 0) or 0)
        self._main_seal_front_max_index = int(params.get("main_seal_front_max_index", 5) or 5)
        self._front_big_weak_ratio = float(params.get("front_big_weak_ratio", 0.50) or 0.50)
        self._back_big_min_amount = float(params.get("back_big_min_amount", 2_000_000.0) or 2_000_000.0)
        self._max_queue_ms = int(params.get("max_queue_ms", 8_000) or 8_000)
        self._cooldown_ms = int(params.get("cooldown_ms", 5_000) or 5_000)
        self._probe_lots = int(params.get("probe_lots", 1) or 1)
        self._submit_gap_ms = int(params.get("submit_gap_ms", 0) or 0)
        self._probe_wait_timeout_ms = int(params.get("probe_wait_timeout_ms", 8_000) or 8_000)
        self._cancel_before_probe_fill_on_timeout = bool(params.get("cancel_before_probe_fill_on_timeout", False))
        self._probe_decision_window_ms = int(params.get("probe_decision_window_ms", 1_200) or 1_200)
        self._post_probe_keep_ms = int(params.get("post_probe_keep_ms", 5_000) or 5_000)
        self._cancel_to_add_ratio_max = float(params.get("cancel_to_add_ratio_max", 0.60) or 0.60)
        self._bid1_volume_drop_ratio_max = float(params.get("bid1_volume_drop_ratio_max", 0.35) or 0.35)
        self._front50_depth_drop_ratio_max = float(params.get("front50_depth_drop_ratio_max", 0.35) or 0.35)
        self._unknown_cancel_risk_amount = float(params.get("unknown_cancel_risk_amount", 500_000.0) or 500_000.0)
        self._min_limit_buy_net_amount = float(params.get("min_limit_buy_net_amount", 0.0) or 0.0)
        self._l2_calibration_enabled = bool(
            params.get(
                "l2_calibration_enabled",
                getattr(global_settings, "CYTRADE_MAIN_SEAL_FOLLOW_L2_CALIBRATION", False),
            )
        )
        self._l2_calibration_dir = str(
            params.get(
                "l2_calibration_dir",
                getattr(global_settings, "CYTRADE_MAIN_SEAL_FOLLOW_L2_CALIBRATION_DIR", "") or "",
            )
            or ""
        )
        self._warning_throttle_ms = int(params.get("warning_throttle_ms", 60_000) or 60_000)
        self._warning_last_emit_ms: Dict[str, int] = {}
        self._entry_disabled_reason = str(params.get("entry_disabled_reason") or "")

        self._entry_state = self.STATE_WAIT_SIGNAL
        self._limit_up_price = float(params.get("limit_up_price", 0.0) or 0.0)
        self._last_price = float(params.get("last_price", 0.0) or 0.0)
        self._target_lots = int(params.get("target_lots", 0) or 0)
        self._target_shares = int(params.get("target_shares", 0) or 0)
        self._feature_split_lots: List[int] = list(params.get("feature_split_lots", []) or [])
        self._entry_order_uuids: List[str] = list(params.get("entry_order_uuids", []) or [])
        self._probe_order_uuid = str(params.get("probe_order_uuid") or "")
        self._main_order_uuids: List[str] = list(params.get("main_order_uuids", []) or [])
        self._probe_submit_time_ms = int(params.get("probe_submit_time_ms", 0) or 0)
        self._probe_fill_time_ms = int(params.get("probe_fill_time_ms", 0) or 0)
        self._probe_filled_quantity = int(params.get("probe_filled_quantity", 0) or 0)
        self._main_keep_decision_time_ms = int(params.get("main_keep_decision_time_ms", 0) or 0)
        self._last_decision_metrics: Dict[str, object] = dict(params.get("last_decision_metrics", {}) or {})
        self._current_queue: List[int] = list(params.get("current_queue", []) or [])
        self._current_queue_time_ms = int(params.get("current_queue_time_ms", 0) or 0)
        self._current_queue_observed_count = int(params.get("current_queue_observed_count", 0) or 0)
        self._current_queue_reported_count = int(params.get("current_queue_reported_count", 0) or 0)
        self._current_queue_is_partial = bool(params.get("current_queue_is_partial", False))
        self._current_front50_depth_lot = int(params.get("current_front50_depth_lot", 0) or 0)
        self._current_bid1_volume_lot = int(params.get("current_bid1_volume_lot", 0) or 0)
        self._current_bid_level_number = int(params.get("current_bid_level_number", 0) or 0)
        self._existing_limit_seen_since_ms = int(params.get("existing_limit_seen_since_ms", 0) or 0)
        self._l2_detail_enabled = bool(params.get("l2_detail_enabled", False))
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
        self._recent_limit_buy_add_orders: Deque[dict] = deque(maxlen=1000)
        self._recent_limit_sell_add_orders: Deque[dict] = deque(maxlen=1000)
        self._recent_limit_buy_cancel_orders: Deque[dict] = deque(maxlen=1000)
        self._recent_unknown_cancel_orders: Deque[dict] = deque(maxlen=1000)
        self._recent_limit_trades: Deque[dict] = deque(maxlen=1000)
        self._recent_quote_points: Deque[dict] = deque(maxlen=500)
        self._recent_queue_points: Deque[dict] = deque(maxlen=500)
        self._l2_order_index: Dict[str, dict] = dict(params.get("l2_order_index", {}) or {})
        self._l2_order_index_keys: Deque[str] = deque(
            list(params.get("l2_order_index_keys", []) or []),
            maxlen=20_000,
        )
        self._l2_calibration_lock = threading.Lock()
        self._l2_calibration_path = self._resolve_l2_calibration_path()

    @classmethod
    def required_data_kinds(cls) -> set[str]:
        return {"l2quote"}

    def current_data_kinds(self) -> set[str]:
        kinds = {"l2quote"}
        if self._needs_l2_detail_subscription():
            kinds.update({"l2transaction", "l2order", "l2orderqueue"})
        return kinds

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
                                "dry_run_replay_probe_logic": self._dry_run_replay_probe_logic,
                                "big_amount_min": self._big_amount_min,
                                "queue_vol_unit": self._queue_vol_unit,
                                "order_vol_unit": self._order_vol_unit,
                                "trade_vol_unit": self._trade_vol_unit,
                                "sweep_window_ms": self._sweep_window_ms,
                                "sweep_near_limit_ticks": self._sweep_near_limit_ticks,
                                "sweep_min_amount": self._sweep_min_amount,
                                "quote_trigger_near_limit_ticks": self._quote_trigger_near_limit_ticks,
                                "main_seal_window_ms": self._main_seal_window_ms,
                                "require_recent_big_limit_buy": self._require_recent_big_limit_buy,
                                "block_on_recent_big_limit_cancel": self._block_on_recent_big_limit_cancel,
                                "allow_existing_limit_queue": self._allow_existing_limit_queue,
                                "existing_limit_observe_ms": self._existing_limit_observe_ms,
                                "existing_limit_min_bid1_lot": self._existing_limit_min_bid1_lot,
                                "existing_limit_min_reported_orders": self._existing_limit_min_reported_orders,
                                "main_seal_front_max_index": self._main_seal_front_max_index,
                                "front_big_weak_ratio": self._front_big_weak_ratio,
                                "back_big_min_amount": self._back_big_min_amount,
                                "max_queue_ms": self._max_queue_ms,
                                "cooldown_ms": self._cooldown_ms,
                                "probe_lots": self._probe_lots,
                                "submit_gap_ms": self._submit_gap_ms,
                                "probe_wait_timeout_ms": self._probe_wait_timeout_ms,
                                "cancel_before_probe_fill_on_timeout": self._cancel_before_probe_fill_on_timeout,
                                "probe_decision_window_ms": self._probe_decision_window_ms,
                                "post_probe_keep_ms": self._post_probe_keep_ms,
                                "cancel_to_add_ratio_max": self._cancel_to_add_ratio_max,
                                "bid1_volume_drop_ratio_max": self._bid1_volume_drop_ratio_max,
                                "front50_depth_drop_ratio_max": self._front50_depth_drop_ratio_max,
                                "unknown_cancel_risk_amount": self._unknown_cancel_risk_amount,
                                "min_limit_buy_net_amount": self._min_limit_buy_net_amount,
                                "l2_calibration_enabled": self._l2_calibration_enabled,
                                "l2_calibration_dir": self._l2_calibration_dir,
                                "warning_throttle_ms": self._warning_throttle_ms,
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
        self._entry_disabled_reason = ""
        self._warning_last_emit_ms.clear()
        self._entry_order_uuids = []
        self._probe_order_uuid = ""
        self._main_order_uuids = []
        self._probe_submit_time_ms = 0
        self._probe_fill_time_ms = 0
        self._probe_filled_quantity = 0
        self._main_keep_decision_time_ms = 0
        self._last_decision_metrics = {}
        self._current_queue = []
        self._current_queue_time_ms = 0
        self._current_queue_observed_count = 0
        self._current_queue_reported_count = 0
        self._current_queue_is_partial = False
        self._current_front50_depth_lot = 0
        self._current_bid1_volume_lot = 0
        self._current_bid_level_number = 0
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
        self._recent_limit_buy_add_orders.clear()
        self._recent_limit_sell_add_orders.clear()
        self._recent_limit_buy_cancel_orders.clear()
        self._recent_unknown_cancel_orders.clear()
        self._recent_limit_trades.clear()
        self._recent_quote_points.clear()
        self._recent_queue_points.clear()
        self._l2_order_index.clear()
        self._l2_order_index_keys.clear()

        if self._has_position():
            self._entry_state = self.STATE_HAS_POSITION
        elif self._has_active_entry_order():
            self._entry_state = self.STATE_WAIT_PROBE_FILL
        elif self._entry_state != self.STATE_DRY_RUN_READY:
            self._entry_state = self.STATE_WAIT_SIGNAL
        self._l2_detail_enabled = self._entry_state not in (self.STATE_WAIT_SIGNAL, self.STATE_DRY_RUN_READY)
        return True

    def on_l2_quote(self, event: L2QuoteEvent) -> None:
        self._write_l2_calibration_sample(
            "l2quote",
            event.raw_xt_fields,
            {
                "last_price": float(event.last_price or 0.0),
                "pre_close": float(event.pre_close or 0.0),
                "bid1": float(event.bid1 or 0.0),
                "ask1": float(event.ask1 or 0.0),
                "bid1_volume": int(getattr(event, "bid1_volume", 0) or 0),
                "ask1_volume": int(getattr(event, "ask1_volume", 0) or 0),
                "limit_up_price": float(event.limit_up_price or 0.0),
            },
            event.event_time,
        )
        self._last_price = float(event.last_price or self._last_price or 0.0)
        self._current_bid1_volume_lot = int(getattr(event, "bid1_volume", 0) or 0)
        self._recent_quote_points.append(
            {
                "time": self._to_ms(event.event_time),
                "bid1": float(event.bid1 or 0.0),
                "ask1": float(event.ask1 or 0.0),
                "bid1_volume_lot": self._current_bid1_volume_lot,
            }
        )
        if float(event.limit_up_price or 0.0) > 0:
            self._limit_up_price = float(event.limit_up_price)
        elif float(event.pre_close or 0.0) > 0:
            self._limit_up_price = self._calc_limit_up(self.stock_code, float(event.pre_close))

        if self._plan_amount > 0 and self._limit_up_price > 0 and self._target_lots <= 0:
            self._refresh_target_from_limit_price(self._limit_up_price)
            self._precheck_one_lot_affordability(self._limit_up_price)
        if self._should_enable_l2_detail_from_quote(event):
            self._l2_detail_enabled = True
        self._evaluate_active_entry_market("l2quote")
        if self._should_check_entry_on_quote():
            self._maybe_trigger_entry("l2quote")

    def on_l2_transaction(self, event: L2TransactionEvent) -> None:
        shares = self._trade_vol_to_shares(event.volume)
        amount = float(event.amount or self._amount_of(float(event.price or 0.0), shares))
        is_cancel_transaction = int(getattr(event, "trade_flag", 0) or 0) == 3
        self._write_l2_calibration_sample(
            "l2transaction",
            event.raw_xt_fields,
            {
                "price": float(event.price or 0.0),
                "volume_raw": int(event.volume or 0),
                "volume_shares": shares,
                "amount": amount,
                "side": str(event.side or ""),
                "trade_index": str(getattr(event, "trade_index", "") or ""),
                "buy_no": str(getattr(event, "buy_no", "") or ""),
                "sell_no": str(getattr(event, "sell_no", "") or ""),
                "trade_type": getattr(event, "trade_type", None),
                "trade_flag": getattr(event, "trade_flag", None),
                "is_cancel_transaction": is_cancel_transaction,
                "trade_vol_unit": self._trade_vol_unit,
            },
            event.event_time,
        )
        if is_cancel_transaction:
            self._handle_l2_cancel_transaction(event, shares)
            self._evaluate_active_entry_market("l2transaction_cancel")
            return

        self._recent_trades.append(
            {
                "time": self._to_ms(event.event_time),
                "price": float(event.price or 0.0),
                "volume": shares,
                "amount": amount,
                "side": str(event.side or ""),
            }
        )
        if self._limit_up_price > 0 and self._price_eq(float(event.price or 0.0), self._limit_up_price):
            self._recent_limit_trades.append(
                {
                    "time": self._to_ms(event.event_time),
                    "price": float(event.price or 0.0),
                    "volume": shares,
                    "amount": amount,
                    "side": str(event.side or ""),
                    "trade_index": str(getattr(event, "trade_index", "") or ""),
                }
            )
        self._evaluate_active_entry_market("l2transaction")
        self._maybe_trigger_entry("l2transaction")

    def on_l2_order(self, event: L2OrderEvent) -> None:
        if self._limit_up_price <= 0:
            return
        shares = self._order_vol_to_shares(event.volume)
        amount = float(event.amount or self._amount_of(float(event.price or 0.0), shares))
        self._remember_l2_order(event, shares, amount)
        self._write_l2_calibration_sample(
            "l2order",
            event.raw_xt_fields,
            {
                "price": float(event.price or 0.0),
                "volume_raw": int(event.volume or 0),
                "volume_shares": shares,
                "amount": amount,
                "side": str(event.side or ""),
                "entrust_no": str(event.entrust_no or ""),
                "entrust_type": getattr(event, "entrust_type", None),
                "entrust_direction": getattr(event, "entrust_direction", None),
                "is_cancel": bool(event.is_cancel),
                "order_vol_unit": self._order_vol_unit,
            },
            event.event_time,
        )
        if not self._price_eq(float(event.price or 0.0), self._limit_up_price):
            self._evaluate_active_entry_market("l2order")
            return

        payload = {
            "time": self._to_ms(event.event_time),
            "price": float(event.price or 0.0),
            "volume": shares,
            "amount": amount,
            "entrust_no": str(event.entrust_no or ""),
            "side": str(event.side or ""),
        }
        if self._is_buy_cancel_side(str(event.side or "")) or (bool(event.is_cancel) and self._is_buy_side(str(event.side or ""))):
            self._recent_limit_buy_cancel_orders.append(payload)
            if amount >= self._big_amount_min:
                self._recent_big_limit_cancel_orders.append(payload)
        elif self._is_buy_side(str(event.side or "")):
            self._recent_limit_buy_add_orders.append(payload)
            if amount >= self._big_amount_min:
                self._recent_big_limit_buy_orders.append(payload)
        else:
            self._recent_limit_sell_add_orders.append(payload)
        self._evaluate_active_entry_market("l2order")
        self._maybe_trigger_entry("l2order")

    def on_l2_orderqueue(self, event: L2OrderQueueEvent) -> None:
        queue_price = float(event.price or 0.0)
        self._current_queue_time_ms = self._to_ms(event.event_time)
        normalized_queue = self._queue_to_shares_list(event.bid_level_volume or [])
        self._current_queue_observed_count = int(
            getattr(event, "observed_queue_count", 0) or len(event.bid_level_volume or [])
        )
        self._current_queue_reported_count = int(getattr(event, "reported_total_order_count", 0) or 0)
        self._current_queue_is_partial = bool(getattr(event, "is_partial_queue", False))
        self._current_front50_depth_lot = sum(int(v or 0) for v in list(event.bid_level_volume or []))
        self._current_bid_level_number = self._current_queue_reported_count
        self._recent_queue_points.append(
            {
                "time": self._current_queue_time_ms,
                "price": queue_price,
                "observed_queue_count": self._current_queue_observed_count,
                "reported_total_order_count": self._current_queue_reported_count,
                "is_partial_queue": self._current_queue_is_partial,
                "front50_depth_lot": self._current_front50_depth_lot,
            }
        )
        self._write_l2_calibration_sample(
            "l2orderqueue",
            event.raw_xt_fields,
            {
                "price": queue_price,
                "bid_level_volume_raw": list(event.bid_level_volume or []),
                "bid_level_volume_shares": normalized_queue,
                "observed_queue_count": self._current_queue_observed_count,
                "reported_total_order_count": self._current_queue_reported_count,
                "is_partial_queue": self._current_queue_is_partial,
                "front50_depth_lot": self._current_front50_depth_lot,
                "queue_vol_unit": self._queue_vol_unit,
            },
            event.event_time,
        )
        if self._limit_up_price > 0 and queue_price > 0 and not self._price_eq(queue_price, self._limit_up_price):
            self._current_queue = []
            self._evaluate_active_entry_market("l2orderqueue")
            return
        self._current_queue = normalized_queue
        self._evaluate_active_entry_market("l2orderqueue")
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
            self._precheck_one_lot_affordability(limit_price)
            return []

        planned_parts = self._plan_entry_parts(limit_price, trigger_reason=trigger_reason)
        if not planned_parts:
            logger.warning(
                "MainSealFollow[%s]: no valid split orders generated at limit price %.3f",
                self.strategy_id[:8],
                limit_price,
            )
            return []

        self._queue_before_send = list(self._current_queue)
        self._front_qty_anchor = sum(self._queue_before_send)
        self._probe_order_uuid = ""
        self._main_order_uuids = []
        self._probe_submit_time_ms = self._now_ms()
        self._probe_fill_time_ms = 0
        self._probe_filled_quantity = 0
        self._main_keep_decision_time_ms = 0
        self._last_decision_metrics = {}

        if self._dry_run:
            if not self._dry_run_replay_probe_logic:
                self._entry_state = self.STATE_DRY_RUN_READY
                logger.info(
                    "MainSealFollow[%s] [DRY_RUN]: ready to submit %d entry orders stock=%s name=%s state=%s limit=%.3f shares=%d parts=%s reason=%s",
                    self.strategy_id[:8],
                    len(planned_parts),
                    self.stock_code,
                    self._stock_name,
                    self._entry_state,
                    limit_price,
                    self._target_shares,
                    [(item["role"], item["quantity"]) for item in planned_parts],
                    trigger_reason or "manual",
                )
                return []

            orders: List[Order] = []
            for item in planned_parts:
                order = Order(
                    strategy_id=self.strategy_id,
                    strategy_name=self.strategy_name,
                    stock_code=self.stock_code,
                    price=limit_price,
                    quantity=int(item["quantity"]),
                    direction=OrderDirection.BUY,
                    status=OrderStatus.REPORTED,
                    status_msg="[DRY_RUN] virtual reported order",
                    remark=str(item["remark"]),
                )
                self._track_order(order)
                self._entry_order_uuids.append(order.order_uuid)
                role = str(item.get("role") or "")
                if role == "probe" and not self._probe_order_uuid:
                    self._probe_order_uuid = order.order_uuid
                else:
                    self._main_order_uuids.append(order.order_uuid)
                orders.append(order)

            if orders:
                if not self._probe_order_uuid:
                    self._probe_order_uuid = orders[0].order_uuid
                    self._main_order_uuids = [order.order_uuid for order in orders[1:]]
                self._entry_state = self.STATE_WAIT_PROBE_FILL
                self.request_state_persist(reason=f"msf_dry_run_submit:{self.strategy_id}")
            logger.info(
                "MainSealFollow[%s] [DRY_RUN]: virtual submit %d entry orders stock=%s name=%s state=%s limit=%.3f shares=%d parts=%s reason=%s",
                self.strategy_id[:8],
                len(planned_parts),
                self.stock_code,
                self._stock_name,
                self._entry_state,
                limit_price,
                self._target_shares,
                [(item["role"], item["quantity"]) for item in planned_parts],
                trigger_reason or "manual",
            )
            return orders

        orders: List[Order] = []
        for item in planned_parts:
            if orders and self._submit_gap_ms > 0:
                time.sleep(float(self._submit_gap_ms) / 1000.0)
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
            role = str(item.get("role") or "")
            if role == "probe" and not self._probe_order_uuid:
                self._probe_order_uuid = order.order_uuid
            else:
                self._main_order_uuids.append(order.order_uuid)
            orders.append(order)

        if orders:
            if not self._probe_order_uuid:
                self._probe_order_uuid = orders[0].order_uuid
                self._main_order_uuids = [order.order_uuid for order in orders[1:]]
            self._entry_state = self.STATE_CHAIN_SUBMITTED
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
            self._probe_order_uuid = ""
            self._main_order_uuids = []
            self._probe_submit_time_ms = 0
            self._probe_fill_time_ms = 0
            self._probe_filled_quantity = 0
            self._main_keep_decision_time_ms = 0
            self._last_decision_metrics = {}
            self._queue_before_send = []
            self._front_qty_anchor = 0
            self._my_start = 0
            self._my_end = 0
            self._entry_queue = []
            self._entry_position = {}
            self._position_method = ""
            self._position_confidence = ""
            self._queue_enter_time_ms = 0
            self._existing_limit_seen_since_ms = 0

    def persistent_instance_fields(self) -> List[str]:
        return [
            "_csv_path",
            "_stock_name",
            "_plan_amount",
            "_dry_run",
            "_dry_run_replay_probe_logic",
            "_big_amount_min",
            "_queue_vol_unit",
            "_order_vol_unit",
            "_trade_vol_unit",
            "_sweep_window_ms",
            "_sweep_near_limit_ticks",
            "_sweep_min_amount",
            "_quote_trigger_near_limit_ticks",
            "_main_seal_window_ms",
            "_require_recent_big_limit_buy",
            "_block_on_recent_big_limit_cancel",
            "_allow_existing_limit_queue",
            "_existing_limit_observe_ms",
            "_existing_limit_min_bid1_lot",
            "_existing_limit_min_reported_orders",
            "_main_seal_front_max_index",
            "_front_big_weak_ratio",
            "_back_big_min_amount",
            "_max_queue_ms",
            "_cooldown_ms",
            "_probe_lots",
            "_submit_gap_ms",
            "_probe_wait_timeout_ms",
            "_cancel_before_probe_fill_on_timeout",
            "_probe_decision_window_ms",
            "_post_probe_keep_ms",
            "_cancel_to_add_ratio_max",
            "_bid1_volume_drop_ratio_max",
            "_front50_depth_drop_ratio_max",
            "_unknown_cancel_risk_amount",
            "_min_limit_buy_net_amount",
            "_l2_calibration_enabled",
            "_l2_calibration_dir",
            "_warning_throttle_ms",
            "_entry_disabled_reason",
            "_entry_state",
            "_limit_up_price",
            "_last_price",
            "_target_lots",
            "_target_shares",
            "_feature_split_lots",
            "_entry_order_uuids",
            "_probe_order_uuid",
            "_main_order_uuids",
            "_probe_submit_time_ms",
            "_probe_fill_time_ms",
            "_probe_filled_quantity",
            "_main_keep_decision_time_ms",
            "_last_decision_metrics",
            "_current_queue",
            "_current_queue_time_ms",
            "_current_queue_observed_count",
            "_current_queue_reported_count",
            "_current_queue_is_partial",
            "_current_front50_depth_lot",
            "_current_bid1_volume_lot",
            "_current_bid_level_number",
            "_existing_limit_seen_since_ms",
            "_l2_detail_enabled",
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
        state["recent_limit_buy_add_orders"] = list(self._recent_limit_buy_add_orders)
        state["recent_limit_sell_add_orders"] = list(self._recent_limit_sell_add_orders)
        state["recent_limit_buy_cancel_orders"] = list(self._recent_limit_buy_cancel_orders)
        state["recent_unknown_cancel_orders"] = list(self._recent_unknown_cancel_orders)
        state["recent_limit_trades"] = list(self._recent_limit_trades)
        state["recent_quote_points"] = list(self._recent_quote_points)
        state["recent_queue_points"] = list(self._recent_queue_points)
        state["l2_order_index"] = dict(self._l2_order_index)
        state["l2_order_index_keys"] = list(self._l2_order_index_keys)
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
        self._recent_limit_buy_add_orders = deque(list(state.get("recent_limit_buy_add_orders", []) or []), maxlen=1000)
        self._recent_limit_sell_add_orders = deque(
            list(state.get("recent_limit_sell_add_orders", []) or []),
            maxlen=1000,
        )
        self._recent_limit_buy_cancel_orders = deque(
            list(state.get("recent_limit_buy_cancel_orders", []) or []),
            maxlen=1000,
        )
        self._recent_unknown_cancel_orders = deque(
            list(state.get("recent_unknown_cancel_orders", []) or []),
            maxlen=1000,
        )
        self._recent_limit_trades = deque(list(state.get("recent_limit_trades", []) or []), maxlen=1000)
        self._recent_quote_points = deque(list(state.get("recent_quote_points", []) or []), maxlen=500)
        self._recent_queue_points = deque(list(state.get("recent_queue_points", []) or []), maxlen=500)
        self._l2_order_index = dict(state.get("l2_order_index", {}) or {})
        self._l2_order_index_keys = deque(list(state.get("l2_order_index_keys", []) or []), maxlen=20_000)

    def _on_order_update_hook(self, order: Order) -> None:
        if order.direction != OrderDirection.BUY:
            if order.direction == OrderDirection.SELL and not self._has_position():
                self._entry_state = self.STATE_EXITED
            return

        if order.order_uuid not in self._entry_order_uuids:
            self._entry_order_uuids.append(order.order_uuid)

        if order.order_uuid == self._probe_order_uuid and self._order_has_any_fill(order):
            if self._probe_fill_time_ms <= 0:
                self._probe_fill_time_ms = self._now_ms()
                self._probe_filled_quantity = int(getattr(order, "filled_quantity", 0) or 0)
                self._entry_state = self.STATE_PROBE_FILLED_DECISION
                self._decide_main_after_probe_fill()
            return

        if order.order_uuid in set(self._main_order_uuids) and self._order_has_any_fill(order):
            self._entry_state = self.STATE_HAS_POSITION
            self._cancel_remaining_main_orders("main_deal_happened_cancel_remaining")
            return

        if self._has_position() or self._has_any_recorded_entry_fill():
            if self._probe_fill_time_ms > 0 and self._has_active_main_order():
                self._entry_state = self.STATE_MAIN_KEEPING
            else:
                self._entry_state = self.STATE_HAS_POSITION
            return

        if order.status in (
            OrderStatus.UNREPORTED,
            OrderStatus.WAIT_REPORTING,
            OrderStatus.REPORTED,
        ):
            self._entry_state = self.STATE_WAIT_PROBE_FILL
            self._evaluate_active_entry_market("order_update")
            return

        if order.status in (
            OrderStatus.REPORTED_CANCEL,
            OrderStatus.PARTSUCC_CANCEL,
            OrderStatus.PART_SUCC,
        ):
            self._entry_state = self.STATE_MAIN_CANCELING if self._has_pending_cancel_entry_order() else self.STATE_WAIT_PROBE_FILL
            self._evaluate_active_entry_market("order_update")
            return

        if order.status in (
            OrderStatus.CANCELED,
            OrderStatus.PART_CANCEL,
            OrderStatus.JUNK,
            OrderStatus.UNKNOWN,
        ) and not self._has_active_entry_order():
            self._entry_state = self.STATE_HAS_POSITION if self._has_any_recorded_entry_fill() else self.STATE_WAIT_SIGNAL

    def _refresh_target_from_limit_price(self, limit_price: float) -> None:
        self._target_lots = self._calc_target_lots(self._plan_amount, limit_price)
        self._target_shares = self._target_lots * 100
        self._feature_split_lots = self._make_two_layer_lots(self._target_lots, self._probe_lots)

    def _precheck_one_lot_affordability(self, limit_price: float) -> bool:
        if limit_price <= 0 or self._plan_amount <= 0:
            return True
        if self._target_lots > 0 and self._target_shares > 0:
            return True

        required_amount = float(limit_price) * 100
        self._entry_disabled_reason = "planned_amount_below_one_lot"
        self._l2_detail_enabled = False
        self._throttled_warning(
            "planned_amount_below_one_lot",
            (
                "MainSealFollow[%s]: entry disabled stock=%s name=%s state=%s reason=%s "
                "plan_amount=%.2f required_amount=%.2f limit_price=%.3f"
            ),
            self.strategy_id[:8],
            self.stock_code,
            self._stock_name,
            self._entry_state,
            self._entry_disabled_reason,
            self._plan_amount,
            required_amount,
            limit_price,
        )
        return False

    def _throttled_warning(self, key: str, message: str, *args, interval_ms: Optional[int] = None) -> bool:
        now_ms = self._now_ms()
        interval = int(interval_ms if interval_ms is not None else self._warning_throttle_ms)
        last_ms = int(self._warning_last_emit_ms.get(key, 0) or 0)
        if last_ms > 0 and now_ms - last_ms < interval:
            return False
        self._warning_last_emit_ms[key] = now_ms
        logger.warning(message, *args)
        return True

    def _plan_entry_parts(self, limit_price: float, trigger_reason: str = "") -> List[Dict[str, object]]:
        if not self._feature_split_lots:
            self._refresh_target_from_limit_price(limit_price)
        total_lots = int(self._target_lots or 0)
        if total_lots <= 0:
            return []

        probe_lots = max(1, min(int(self._probe_lots or 1), total_lots))
        main_lots = max(0, total_lots - probe_lots)
        plan = [("probe", probe_lots)]
        if main_lots > 0:
            plan.append(("main", main_lots))

        parts: List[Dict[str, object]] = []
        total_parts = len(plan)
        for index, (role, lots) in enumerate(plan, start=1):
            quantity = int(lots) * 100
            if quantity <= 0:
                continue
            role_text = "probe" if role == "probe" else "main"
            parts.append(
                {
                    "role": role_text,
                    "quantity": quantity,
                    "remark": (
                        f"MSF {role_text} part={index}/{total_parts} "
                        f"limit={limit_price:.3f} trigger={trigger_reason or 'manual'}"
                    ),
                }
            )
        return parts

    def _maybe_trigger_entry(self, trigger_source: str) -> bool:
        if self._entry_disabled_reason:
            return False
        if self._entry_state != self.STATE_WAIT_SIGNAL:
            return False
        if self._has_position() or self._has_active_entry_order():
            return False
        if self._limit_up_price <= 0 or not self._current_queue:
            return False
        if self._last_cancel_time_ms > 0 and self._now_ms() - self._last_cancel_time_ms < self._cooldown_ms:
            return False
        if not self._main_seal_ok():
            return False
        orders = self.submit_feature_entry_orders(
            self._limit_up_price,
            trigger_reason=f"l2:{trigger_source}",
        )
        return bool(orders) or self._entry_state == self.STATE_DRY_RUN_READY

    def _remember_l2_order(self, event: L2OrderEvent, shares: int, amount: float) -> None:
        entrust_no = str(event.entrust_no or "").strip()
        if not entrust_no:
            return
        side_text = str(event.side or "").upper()
        if bool(event.is_cancel) or self._is_buy_cancel_side(side_text) or side_text == "CANCEL_SELL":
            return
        payload = {
            "time": self._to_ms(event.event_time),
            "price": float(event.price or 0.0),
            "volume": int(shares or 0),
            "amount": float(amount or 0.0),
            "side": str(event.side or ""),
            "entrust_no": entrust_no,
            "entrust_type": getattr(event, "entrust_type", None),
            "entrust_direction": getattr(event, "entrust_direction", None),
        }
        if entrust_no not in self._l2_order_index:
            self._l2_order_index_keys.append(entrust_no)
        self._l2_order_index[entrust_no] = payload
        while len(self._l2_order_index) > self._l2_order_index_keys.maxlen:
            old_key = self._l2_order_index_keys.popleft()
            self._l2_order_index.pop(old_key, None)

    def _handle_l2_cancel_transaction(self, event: L2TransactionEvent, shares: int) -> None:
        event_time_ms = self._to_ms(event.event_time)
        refs = [
            str(getattr(event, "buy_no", "") or "").strip(),
            str(getattr(event, "sell_no", "") or "").strip(),
        ]
        refs = [ref for ref in refs if ref and ref != "0"]
        matched = False
        for ref in refs:
            order = self._l2_order_index.get(ref)
            if not order:
                continue
            matched = True
            price = float(order.get("price", 0.0) or 0.0)
            amount = self._amount_of(price, shares)
            side = str(order.get("side", "") or "")
            payload = {
                "time": event_time_ms,
                "price": price,
                "volume": int(shares or 0),
                "amount": amount,
                "entrust_no": ref,
                "trade_index": str(getattr(event, "trade_index", "") or ""),
                "side": f"CANCEL_{side}" if side and not side.startswith("CANCEL_") else side,
            }
            if self._limit_up_price > 0 and self._price_eq(price, self._limit_up_price) and self._is_buy_side(side):
                self._recent_limit_buy_cancel_orders.append(payload)
                if amount >= self._big_amount_min:
                    self._recent_big_limit_cancel_orders.append(payload)
            elif not self._is_buy_side(side):
                self._recent_unknown_cancel_orders.append(payload)

        if not matched:
            self._recent_unknown_cancel_orders.append(
                {
                    "time": event_time_ms,
                    "price": 0.0,
                    "volume": int(shares or 0),
                    "amount": 0.0,
                    "refs": refs,
                    "trade_index": str(getattr(event, "trade_index", "") or ""),
                    "side": "UNKNOWN_CANCEL",
                }
            )

    def _evaluate_active_entry_market(self, source: str = "") -> str:
        if not self._has_active_entry_order() or self._entry_state in (
            self.STATE_WAIT_SIGNAL,
            self.STATE_DRY_RUN_READY,
            self.STATE_HAS_POSITION,
            self.STATE_EXITED,
            self.STATE_MAIN_CANCELING,
        ):
            return ""

        now_ms = self._now_ms()
        if self._entry_state in (self.STATE_CHAIN_SUBMITTED, self.STATE_WAIT_PROBE_FILL, self.STATE_WAIT_ORDER_ACK):
            if self._maybe_simulate_dry_run_probe_fill(source):
                return "dry_run_probe_fill_simulated"
            if self._probe_submit_time_ms > 0:
                elapsed = now_ms - int(self._probe_submit_time_ms or 0)
                if elapsed > self._probe_wait_timeout_ms and self._should_cancel_before_probe_fill():
                    self._request_cancel_entry_orders("probe_wait_timeout_market_weak")
                    self._entry_state = self.STATE_MAIN_CANCELING
                    return "probe_wait_timeout_market_weak"
            return ""

        if self._entry_state in (self.STATE_PROBE_FILLED_DECISION, self.STATE_MAIN_KEEPING):
            if (
                self._entry_state == self.STATE_MAIN_KEEPING
                and self._main_keep_decision_time_ms > 0
                and now_ms - int(self._main_keep_decision_time_ms or 0) > self._post_probe_keep_ms
            ):
                if self._finish_dry_run_keep_cycle(source):
                    return "dry_run_keep_window_complete"
                return ""
            reason = self._main_cancel_reason_after_probe()
            if reason:
                self._request_cancel_main_orders(reason)
                return reason
            if self._entry_state == self.STATE_PROBE_FILLED_DECISION:
                self._entry_state = self.STATE_MAIN_KEEPING
                self._main_keep_decision_time_ms = now_ms
                logger.info(
                    "MainSealFollow[%s]: keep main order after probe fill source=%s metrics=%s",
                    self.strategy_id[:8],
                    source,
                    self._last_decision_metrics,
                )
            return "main_keep"
        return ""

    def _decide_main_after_probe_fill(self) -> str:
        metrics = self._calc_market_decision_metrics(self._probe_decision_window_ms)
        self._last_decision_metrics = metrics
        reason = self._main_cancel_reason_from_metrics(metrics)
        if reason:
            self._request_cancel_main_orders(reason)
            return reason
        self._entry_state = self.STATE_MAIN_KEEPING
        self._main_keep_decision_time_ms = self._now_ms()
        logger.info(
            "MainSealFollow[%s]: probe filled, keep main order stock=%s name=%s state=%s probe_fill_ms=%s metrics=%s",
            self.strategy_id[:8],
            self.stock_code,
            self._stock_name,
            self._entry_state,
            metrics.get("probe_fill_ms"),
            metrics,
        )
        self.request_state_persist(reason=f"msf_probe_keep:{self.strategy_id}", min_interval_sec=0.0)
        return "main_keep"

    def _should_cancel_before_probe_fill(self) -> bool:
        if not self._cancel_before_probe_fill_on_timeout:
            self._last_decision_metrics = self._calc_market_decision_metrics(self._probe_wait_timeout_ms)
            return False

        metrics = self._calc_market_decision_metrics(self._probe_wait_timeout_ms)
        self._last_decision_metrics = metrics
        if float(metrics.get("limit_buy_cancel_amount", 0.0) or 0.0) > float(
            metrics.get("limit_buy_add_amount", 0.0) or 0.0
        ):
            return True
        if bool(metrics.get("bid1_and_front50_weak")):
            return True
        if bool(metrics.get("unknown_cancel_risk")) and bool(metrics.get("book_weak")):
            return True
        return True

    def _main_cancel_reason_after_probe(self) -> str:
        metrics = self._calc_market_decision_metrics(self._probe_decision_window_ms)
        self._last_decision_metrics = metrics
        return self._main_cancel_reason_from_metrics(metrics)

    def _main_cancel_reason_from_metrics(self, metrics: dict) -> str:
        limit_buy_add = float(metrics.get("limit_buy_add_amount", 0.0) or 0.0)
        limit_buy_cancel = float(metrics.get("limit_buy_cancel_amount", 0.0) or 0.0)
        limit_buy_net = float(metrics.get("limit_buy_net_amount", 0.0) or 0.0)
        cancel_to_add_ratio = float(metrics.get("cancel_to_add_ratio", 0.0) or 0.0)
        limit_trade_amount = float(metrics.get("limit_trade_amount", 0.0) or 0.0)
        unknown_cancel_amount = float(metrics.get("unknown_cancel_amount", 0.0) or 0.0)

        if limit_buy_cancel > 0 and limit_buy_cancel > limit_buy_add:
            return "confirmed_limit_buy_cancel_gt_add"
        if cancel_to_add_ratio > self._cancel_to_add_ratio_max and limit_buy_cancel > 0:
            return "cancel_to_add_ratio_too_high"
        if limit_trade_amount > limit_buy_add and limit_buy_net < self._min_limit_buy_net_amount:
            return "limit_trade_consumption_without_replenish"
        if bool(metrics.get("bid1_and_front50_weak")):
            return "bid1_and_front50_depth_weak"
        if unknown_cancel_amount >= self._unknown_cancel_risk_amount and bool(metrics.get("book_weak")):
            return "unknown_cancel_with_book_weak"
        return ""

    def _calc_market_decision_metrics(self, window_ms: int) -> dict:
        window_ms = int(window_ms or self._main_seal_window_ms or 1000)
        recent_buy_adds = self._recent_items(self._recent_limit_buy_add_orders, window_ms)
        recent_sell_adds = self._recent_items(self._recent_limit_sell_add_orders, window_ms)
        recent_buy_cancels = self._recent_items(self._recent_limit_buy_cancel_orders, window_ms)
        recent_unknown_cancels = self._recent_items(self._recent_unknown_cancel_orders, window_ms)
        recent_limit_trades = self._recent_items(self._recent_limit_trades, window_ms)

        limit_buy_add_amount = sum(float(item.get("amount", 0.0) or 0.0) for item in recent_buy_adds)
        limit_sell_add_amount = sum(float(item.get("amount", 0.0) or 0.0) for item in recent_sell_adds)
        limit_buy_cancel_amount = sum(float(item.get("amount", 0.0) or 0.0) for item in recent_buy_cancels)
        unknown_cancel_amount = sum(float(item.get("amount", 0.0) or 0.0) for item in recent_unknown_cancels)
        unknown_cancel_volume = sum(int(item.get("volume", 0) or 0) for item in recent_unknown_cancels)
        limit_trade_amount = sum(float(item.get("amount", 0.0) or 0.0) for item in recent_limit_trades)
        cancel_to_add_ratio = (
            float(limit_buy_cancel_amount) / float(limit_buy_add_amount)
            if limit_buy_add_amount > 0
            else (999.0 if limit_buy_cancel_amount > 0 else 0.0)
        )

        bid1_drop_ratio = self._calc_recent_drop_ratio(self._recent_quote_points, "bid1_volume_lot", window_ms)
        front50_drop_ratio = self._calc_recent_drop_ratio(self._recent_queue_points, "front50_depth_lot", window_ms)
        bid_level_number_delta = self._calc_recent_delta(self._recent_queue_points, "reported_total_order_count", window_ms)
        metrics = {
            "window_ms": window_ms,
            "probe_fill_ms": (
                int(self._probe_fill_time_ms or 0) - int(self._probe_submit_time_ms or 0)
                if self._probe_fill_time_ms > 0 and self._probe_submit_time_ms > 0
                else 0
            ),
            "limit_buy_add_amount": limit_buy_add_amount,
            "limit_sell_add_amount": limit_sell_add_amount,
            "limit_buy_cancel_amount": limit_buy_cancel_amount,
            "limit_buy_net_amount": limit_buy_add_amount - limit_buy_cancel_amount,
            "cancel_to_add_ratio": cancel_to_add_ratio,
            "limit_trade_amount": limit_trade_amount,
            "unknown_cancel_amount": unknown_cancel_amount,
            "unknown_cancel_volume": unknown_cancel_volume,
            "bid1_volume_drop_ratio": bid1_drop_ratio,
            "front50_depth_drop_ratio": front50_drop_ratio,
            "bid_level_number_delta": bid_level_number_delta,
            "book_weak": bid1_drop_ratio > 0 or front50_drop_ratio > 0 or bid_level_number_delta < 0,
            "bid1_and_front50_weak": (
                bid1_drop_ratio >= self._bid1_volume_drop_ratio_max
                and front50_drop_ratio >= self._front50_depth_drop_ratio_max
            ),
            "current_bid1_volume_lot": self._current_bid1_volume_lot,
            "current_front50_depth_lot": self._current_front50_depth_lot,
            "current_bid_level_number": self._current_bid_level_number,
        }
        return metrics

    def _calc_recent_drop_ratio(self, points: Deque[dict], field: str, window_ms: int) -> float:
        recent = self._recent_items(points, window_ms)
        if len(recent) < 2:
            return 0.0
        first = float(recent[0].get(field, 0.0) or 0.0)
        last = float(recent[-1].get(field, 0.0) or 0.0)
        if first <= 0 or last >= first:
            return 0.0
        return (first - last) / first

    def _calc_recent_delta(self, points: Deque[dict], field: str, window_ms: int) -> float:
        recent = self._recent_items(points, window_ms)
        if len(recent) < 2:
            return 0.0
        return float(recent[-1].get(field, 0.0) or 0.0) - float(recent[0].get(field, 0.0) or 0.0)

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
            if self._dry_run and self._dry_run_replay_probe_logic:
                return self._finalize_dry_run_orders(self._get_active_entry_orders(), reason, scope="entry")
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

    def _request_cancel_main_orders(self, reason: str) -> int:
        cancel_order = getattr(self._trade_executor, "cancel_order", None)
        if not callable(cancel_order):
            if self._dry_run and self._dry_run_replay_probe_logic:
                return self._finalize_dry_run_orders(self._get_active_main_orders(), reason, scope="main")
            return 0
        if (
            reason
            and self._last_cancel_reason == reason
            and self._last_cancel_time_ms > 0
            and self._now_ms() - self._last_cancel_time_ms < 1_000
        ):
            return 0

        submitted = 0
        main_ids = set(self._main_order_uuids)
        for order in self._get_active_entry_orders():
            if main_ids and order.order_uuid not in main_ids:
                continue
            if order.status in (OrderStatus.REPORTED_CANCEL, OrderStatus.PARTSUCC_CANCEL):
                continue
            if bool(cancel_order(order.order_uuid, remark=f"MSF cancel main: {reason}")):
                submitted += 1

        if submitted > 0:
            self._entry_state = self.STATE_MAIN_CANCELING
            self._last_cancel_time_ms = self._now_ms()
            self._last_cancel_reason = str(reason or "")
            self.request_state_persist(reason=f"msf_cancel_main:{self.strategy_id}", min_interval_sec=0.0)
        return submitted

    def _cancel_remaining_entry_orders(self, reason: str) -> int:
        if not self._get_active_entry_orders():
            return 0
        return self._request_cancel_entry_orders(reason)

    def _cancel_remaining_main_orders(self, reason: str) -> int:
        if not self._has_active_main_order():
            return 0
        return self._request_cancel_main_orders(reason)

    def _get_active_entry_orders(self) -> List[Order]:
        return [
            order
            for order in list(self._pending_orders.values())
            if order.direction == OrderDirection.BUY and order.is_active()
        ]

    def _get_active_main_orders(self) -> List[Order]:
        main_ids = set(self._main_order_uuids)
        return [
            order
            for order in self._get_active_entry_orders()
            if not main_ids or order.order_uuid in main_ids
        ]

    def _has_active_main_order(self) -> bool:
        return bool(self._get_active_main_orders())

    def _find_entry_order(self, order_uuid: str) -> Optional[Order]:
        if not order_uuid:
            return None
        order = self._pending_orders.get(order_uuid)
        if order:
            return order
        for item in reversed(list(getattr(self, "_orders_history", []) or [])):
            if str(getattr(item, "order_uuid", "") or "") == str(order_uuid):
                return item
        return None

    def _limit_trade_shares_since(self, since_ms: int) -> int:
        if since_ms <= 0:
            return 0
        return sum(
            int(item.get("volume", 0) or 0)
            for item in list(self._recent_limit_trades)
            if int(item.get("time", 0) or 0) >= int(since_ms)
        )

    def _maybe_simulate_dry_run_probe_fill(self, source: str = "") -> bool:
        if not self._dry_run or not self._dry_run_replay_probe_logic:
            return False
        if self._probe_fill_time_ms > 0 or self._probe_submit_time_ms <= 0:
            return False
        probe_order = self._find_entry_order(self._probe_order_uuid)
        if not probe_order or not probe_order.is_active():
            return False

        probe_qty = int(getattr(probe_order, "quantity", 0) or 0)
        traded_shares = self._limit_trade_shares_since(self._probe_submit_time_ms)
        threshold_shares = max(probe_qty, int(self._front_qty_anchor or 0) + probe_qty)
        if traded_shares < threshold_shares:
            return False

        probe_order.status = OrderStatus.SUCCEEDED
        probe_order.status_msg = f"[DRY_RUN] simulated probe fill source={source or 'market'}"
        probe_order.update_time = datetime.now()
        self._pending_orders.pop(probe_order.order_uuid, None)
        self._probe_fill_time_ms = self._now_ms()
        self._probe_filled_quantity = probe_qty
        self._entry_state = self.STATE_PROBE_FILLED_DECISION
        logger.info(
            "MainSealFollow[%s] [DRY_RUN]: simulated probe fill stock=%s name=%s state=%s source=%s traded_shares=%d threshold_shares=%d",
            self.strategy_id[:8],
            self.stock_code,
            self._stock_name,
            self._entry_state,
            source or "market",
            traded_shares,
            threshold_shares,
        )
        self._decide_main_after_probe_fill()
        self.request_state_persist(reason=f"msf_dry_run_probe_fill:{self.strategy_id}", min_interval_sec=0.0)
        return True

    def _finalize_dry_run_orders(self, orders: List[Order], reason: str, scope: str = "") -> int:
        if not orders:
            return 0
        if (
            reason
            and self._last_cancel_reason == reason
            and self._last_cancel_time_ms > 0
            and self._now_ms() - self._last_cancel_time_ms < 1_000
        ):
            return 0

        finalized = 0
        for order in list(orders):
            if not order.is_active():
                continue
            order.status = OrderStatus.CANCELED
            order.status_msg = f"[DRY_RUN] {scope or 'entry'} closed: {reason}"
            order.update_time = datetime.now()
            self.on_order_update(order)
            finalized += 1

        if finalized > 0:
            self._last_cancel_time_ms = self._now_ms()
            self._last_cancel_reason = str(reason or "")
            logger.info(
                "MainSealFollow[%s] [DRY_RUN]: finalize %s orders=%d stock=%s name=%s state=%s reason=%s",
                self.strategy_id[:8],
                scope or "entry",
                finalized,
                self.stock_code,
                self._stock_name,
                self._entry_state,
                reason or "",
            )
            self.request_state_persist(reason=f"msf_dry_run_finalize:{self.strategy_id}", min_interval_sec=0.0)
        return finalized

    def _finish_dry_run_keep_cycle(self, source: str = "") -> bool:
        if not self._dry_run or not self._dry_run_replay_probe_logic:
            return False
        orders = self._get_active_main_orders()
        if not orders:
            return False
        logger.info(
            "MainSealFollow[%s] [DRY_RUN]: keep window complete stock=%s name=%s state=%s source=%s metrics=%s",
            self.strategy_id[:8],
            self.stock_code,
            self._stock_name,
            self._entry_state,
            source or "market",
            self._last_decision_metrics,
        )
        return bool(self._finalize_dry_run_orders(orders, "dry_run_keep_window_complete", scope="main"))

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

    def _resolve_l2_calibration_path(self) -> str:
        if not self._l2_calibration_enabled:
            return ""
        base_dir = str(self._l2_calibration_dir or "").strip() or os.path.join(global_settings.LOG_DIR, "l2_calibration")
        safe_code = str(self.stock_code or "unknown").strip() or "unknown"
        safe_name = str(self._stock_name or self.strategy_name).strip().replace(" ", "_")
        return str(Path(base_dir) / f"main_seal_follow_{safe_code}_{safe_name}.jsonl")

    def _write_l2_calibration_sample(
        self,
        event_type: str,
        raw_fields: dict,
        normalized: dict,
        event_time=None,
    ) -> None:
        if not self._l2_calibration_path:
            return
        path = Path(self._l2_calibration_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "event_type": str(event_type or ""),
            "stock_code": self.stock_code,
            "stock_name": self._stock_name,
            "strategy_id": self.strategy_id,
            "event_time_ms": self._to_ms(event_time),
            "units": {
                "queue_vol_unit": self._queue_vol_unit,
                "order_vol_unit": self._order_vol_unit,
                "trade_vol_unit": self._trade_vol_unit,
            },
            "normalized": normalized,
            "raw_xt_fields": self._json_safe(raw_fields or {}),
        }
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._l2_calibration_lock:
            with path.open("a", encoding="utf-8") as fp:
                fp.write(line)

    @staticmethod
    def _json_safe(value):
        if isinstance(value, datetime):
            return value.isoformat(timespec="milliseconds")
        if isinstance(value, dict):
            return {str(k): MainSealFollowStrategy._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [MainSealFollowStrategy._json_safe(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

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
    def _make_two_layer_lots(total_lots: int, probe_lots: int = 1) -> List[int]:
        total_lots = int(total_lots or 0)
        if total_lots <= 0:
            return []
        probe = max(1, min(int(probe_lots or 1), total_lots))
        main = max(0, total_lots - probe)
        return [probe] if main <= 0 else [probe, main]

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
        return text in {"B", "BUY", "23", "1"} or (("BUY" in text) and ("CANCEL" not in text))

    @staticmethod
    def _is_buy_cancel_side(value: str) -> bool:
        text = str(value or "").strip().upper()
        return text in {"CANCEL_BUY", "3"} or "CANCEL_BUY" in text

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

    def _existing_limit_queue_ok(self) -> bool:
        """Allow startup on an already sealed limit-up board after a short stable observation."""
        if not self._allow_existing_limit_queue:
            self._existing_limit_seen_since_ms = 0
            return False
        if self._limit_up_price <= 0 or not self._current_queue:
            self._existing_limit_seen_since_ms = 0
            return False
        if self._existing_limit_min_bid1_lot > 0 and self._current_bid1_volume_lot < self._existing_limit_min_bid1_lot:
            self._existing_limit_seen_since_ms = 0
            return False
        if (
            self._existing_limit_min_reported_orders > 0
            and self._current_queue_reported_count < self._existing_limit_min_reported_orders
        ):
            self._existing_limit_seen_since_ms = 0
            return False
        bid1_price = 0.0
        if self._recent_quote_points:
            bid1_price = float(self._recent_quote_points[-1].get("bid1", 0.0) or 0.0)
        if not self._price_eq(bid1_price, self._limit_up_price):
            self._existing_limit_seen_since_ms = 0
            return False

        now_ms = self._now_ms()
        if self._existing_limit_seen_since_ms <= 0:
            self._existing_limit_seen_since_ms = now_ms
        return now_ms - self._existing_limit_seen_since_ms >= self._existing_limit_observe_ms

    def _main_seal_ok(self) -> bool:
        if self._limit_up_price <= 0 or not self._current_queue:
            self._existing_limit_seen_since_ms = 0
            return False
        if not self._detect_main_seal_from_queue(self._current_queue, self._limit_up_price):
            self._existing_limit_seen_since_ms = 0
            return False
        if self._block_on_recent_big_limit_cancel and self._recent_big_limit_cancel_blocked():
            self._existing_limit_seen_since_ms = 0
            return False
        if self._require_recent_big_limit_buy and not self._recent_big_limit_buy_ok():
            return self._existing_limit_queue_ok()
        self._existing_limit_seen_since_ms = 0
        return True

    def _needs_l2_detail_subscription(self) -> bool:
        if self._entry_disabled_reason:
            self._l2_detail_enabled = False
            return False
        if self._entry_state in (self.STATE_HAS_POSITION, self.STATE_EXITED) or self._has_position():
            self._l2_detail_enabled = False
            return False
        if self._entry_state != self.STATE_WAIT_SIGNAL:
            return True
        if self._has_active_entry_order():
            return True
        if self._l2_detail_enabled:
            return True
        return False

    def _should_enable_l2_detail_from_quote(self, event: L2QuoteEvent) -> bool:
        if self._entry_disabled_reason:
            return False
        if self._l2_detail_enabled:
            return False
        if self._entry_state != self.STATE_WAIT_SIGNAL:
            return True
        if self._limit_up_price <= 0:
            return False

        near_price = self._limit_up_price - float(self._quote_trigger_near_limit_ticks or 0) * 0.01
        bid1_price = float(getattr(event, "bid1", 0.0) or 0.0)
        last_price = float(getattr(event, "last_price", 0.0) or self._last_price or 0.0)
        return bid1_price >= near_price or last_price >= near_price

    def _should_check_entry_on_quote(self) -> bool:
        if self._entry_disabled_reason:
            return False
        if self._entry_state != self.STATE_WAIT_SIGNAL:
            return False
        if self._has_position() or self._has_active_entry_order():
            return False
        if self._limit_up_price <= 0 or not self._current_queue:
            return False
        near_price = self._limit_up_price - float(self._quote_trigger_near_limit_ticks or 0) * 0.01
        bid1_price = 0.0
        if self._recent_quote_points:
            bid1_price = float(self._recent_quote_points[-1].get("bid1", 0.0) or 0.0)
        return (
            float(self._last_price or 0.0) >= near_price
            or bid1_price >= near_price
        )

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
