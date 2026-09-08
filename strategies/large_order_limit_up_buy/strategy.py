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

    def __init__(self, config: StrategyConfig, trade_executor=None, position_manager=None):
        super().__init__(config, trade_executor, position_manager)
        params = dict(config.params or {})
        self._csv_path = Path(str(params.get("csv_path") or self.DEFAULT_POOL))
        self._stock_name = str(params.get("stock_name") or params.get("name") or "").strip()
        self._plan_amount = float(params.get("plan_amount", 0.0) or 0.0)
        self._dry_run = bool(params.get("dry_run", True))
        self._low_price_threshold = float(params.get("low_price_threshold", 8.0) or 8.0)
        self._low_price_min_lots = int(params.get("low_price_min_lots", 5000) or 5000)
        self._normal_price_min_amount = float(params.get("normal_price_min_amount", 5_000_000.0) or 5_000_000.0)
        self._neighbor_count = max(1, int(params.get("neighbor_count", 5) or 5))
        self._neighbor_window_seconds = max(0.0, float(params.get("neighbor_window_seconds", 3.0) or 3.0))
        self._record_dir = Path(str(params.get("record_dir") or self.DEFAULT_RECORD_DIR))
        self._limit_up_price = 0.0
        self._limit_up_price_warning_logged = False
        self._last_limit_up_lookup = 0.0
        self._initial_quote_checked = False
        self._entry_phase = "WAIT_INITIAL_QUOTE"
        self._pre_close = 0.0
        self._last_quote: L2QuoteEvent | None = None
        self._orders_by_no: dict[str, dict[str, Any]] = {}
        self._queue_orders: deque[dict[str, Any]] = deque(maxlen=2000)
        self._seen_entrust_nos: set[str] = set()
        self._active_order_uuid = ""
        self._active_trigger_entrust_no = ""
        self._trigger_count = 0
        self._sealed_trade_amount = 0.0
        self._recent_trades: deque[tuple[float, float]] = deque(maxlen=5000)
        self._last_queue: L2OrderQueueEvent | None = None
        self._raw_handle = None
        self._neighbor_handle = None
        self._snapshot_handle = None

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
                        "low_price_threshold": self._low_price_threshold,
                        "low_price_min_lots": self._low_price_min_lots,
                        "normal_price_min_amount": self._normal_price_min_amount,
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
        return None

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
        if not self._initial_quote_checked and not self._quote_has_price(event):
            return
        if not self._initial_quote_checked:
            self._initial_quote_checked = True
            if self._quote_is_limit_up(event):
                self._entry_phase = "WAIT_REOPEN"
                self._log_event("startup_already_limit_up", limit_up_price=self._limit_up_price)
            else:
                self._entry_phase = "READY"
        elif self._entry_phase == "WAIT_REOPEN" and self._quote_is_broken(event):
            self._entry_phase = "WAIT_RESEAL"
            self._log_event("limit_up_reopened", limit_up_price=self._limit_up_price)
        self._write_raw("l2quote", event.event_time, event.raw_xt_fields)

    def on_l2_order(self, event: L2OrderEvent) -> None:
        if event.stock_code != self.stock_code:
            return
        self._write_raw("l2order", event.event_time, event.raw_xt_fields)
        if not self._is_continuous_trading_time(event.event_time):
            return
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
        if self._is_big_order(price, volume, amount):
            self._maybe_submit(record)
        self._write_pending_neighbors()

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
                self._orders_by_no[ref]["cancelled_volume"] = int(event.volume or 0)
                self._orders_by_no[ref]["remaining_volume"] = max(
                    0, int(self._orders_by_no[ref]["volume"]) - int(event.volume or 0)
                )
                if ref == self._active_trigger_entrust_no:
                    self._log_event("trigger_order_canceled", trigger_entrust_no=ref)
                    self._active_trigger_entrust_no = ""
        else:
            ref = str(event.buy_no or "").strip()
            if ref in self._orders_by_no:
                self._orders_by_no[ref]["filled_volume"] = int(self._orders_by_no[ref].get("filled_volume", 0)) + int(event.volume or 0)
                self._orders_by_no[ref]["remaining_volume"] = max(
                    0, int(self._orders_by_no[ref]["volume"]) - int(self._orders_by_no[ref]["filled_volume"])
                )

    def on_l2_orderqueue(self, event: L2OrderQueueEvent) -> None:
        if event.stock_code == self.stock_code:
            self._last_queue = event
            self._write_raw("l2orderqueue", event.event_time, event.raw_xt_fields)

    def _maybe_submit(self, trigger: dict[str, Any]) -> None:
        if self._active_order_uuid:
            return
        if self._has_position():
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
        remark = f"L2涨停大单打板 trigger={trigger.get('entrust_no', '')} front={front_volume}股"
        order = self.add_position(price, quantity, remark)
        if order is None:
            self._log_event("buy_blocked", reason="order_executor_unavailable", trigger=trigger)
            return
        self._active_order_uuid = str(order.order_uuid or "")
        self._active_trigger_entrust_no = str(trigger.get("entrust_no", "") or "")
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

    def _on_order_update_hook(self, order) -> None:
        if str(getattr(order, "order_uuid", "")) != self._active_order_uuid:
            return
        status = getattr(order, "status", None)
        if status in (OrderStatus.CANCELED, OrderStatus.PART_CANCEL, OrderStatus.JUNK, OrderStatus.UNKNOWN):
            self._active_order_uuid = ""
            self._log_event("our_order_finished", status=str(status), can_retrigger=True)
        elif status in (OrderStatus.SUCCEEDED, OrderStatus.PART_SUCC):
            self._log_event("our_order_filled", status=str(status), filled_quantity=int(getattr(order, "filled_quantity", 0) or 0), can_retrigger=False)

    def _open_record_files(self) -> None:
        day_dir = self._record_dir / datetime.now().strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{self.stock_code}_{self.strategy_id[:8]}"
        raw_path = day_dir / f"{stem}.l2.jsonl"
        neighbor_path = day_dir / f"{stem}.queue_neighbors.csv"
        snapshot_path = day_dir / f"{stem}.queue_snapshots.csv"
        self._raw_handle = raw_path.open("a", encoding="utf-8")
        self._neighbor_handle = neighbor_path.open("a", encoding="utf-8", newline="")
        self._snapshot_handle = snapshot_path.open("a", encoding="utf-8", newline="")
        if neighbor_path.stat().st_size == 0:
            csv.writer(self._neighbor_handle).writerow(self.NEIGHBOR_HEADERS)
        if snapshot_path.stat().st_size == 0:
            csv.writer(self._snapshot_handle).writerow(self.SNAPSHOT_HEADERS)

    def stop(self) -> None:
        for attr in ("_raw_handle", "_neighbor_handle", "_snapshot_handle"):
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
        lots = volume / 100.0
        if price < self._low_price_threshold:
            return lots > self._low_price_min_lots
        return amount > self._normal_price_min_amount

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

    def _quote_is_limit_up(self, event: L2QuoteEvent) -> bool:
        return any(
            self._is_limit_up_price(price)
            for price in (float(event.last_price or 0.0), float(event.bid1 or 0.0))
            if price > 0
        )

    @staticmethod
    def _quote_has_price(event: L2QuoteEvent) -> bool:
        return any(float(price or 0.0) > 0 for price in (event.last_price, event.bid1))

    def _quote_is_broken(self, event: L2QuoteEvent) -> bool:
        return any(
            0 < price < self._limit_up_price and not self._is_limit_up_price(price)
            for price in (float(event.last_price or 0.0), float(event.bid1 or 0.0))
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
