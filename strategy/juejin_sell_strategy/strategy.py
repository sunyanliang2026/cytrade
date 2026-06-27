"""QMT conversion of the Juejin sell-side tick strategy.

The original strategy under ``docs/掘金/main.py`` is a sell-only strategy driven
by tick-level bid/ask prices and a small CSV file.  This module keeps the same
state-machine shape while routing all orders through the project's
``TradeExecutor`` so dry-run/live safety gates remain centralized.
"""
from __future__ import annotations

import csv
from datetime import datetime, time
from pathlib import Path
from typing import Any, List, Optional

from config.enums import OrderDirection, OrderStatus, StrategyStatus
from core.models import TickData
from monitor.logger import get_logger
from position.models import PositionInfo
from strategy.base import BaseStrategy
from strategy.models import StrategyConfig
from trading.models import Order

logger = get_logger("trade")


class JuejinSellStrategy(BaseStrategy):
    """Sell-side port of the Juejin ``sell_10.csv`` tick strategy."""

    strategy_name = "JuejinSellStrategy"
    max_positions = 200
    max_total_amount = 0.0
    state_version = 1

    def __init__(
        self,
        config: StrategyConfig,
        trade_executor=None,
        position_manager=None,
    ):
        super().__init__(config, trade_executor, position_manager)
        params = dict(self.config.params or {})
        self._source_symbol = str(params.get("source_symbol", "") or "")
        self._nick = str(params.get("nick", "") or self.stock_code)
        self._exp = self._to_int(params.get("exp"), 0)
        self._sell_quantity = max(0, self._to_int(params.get("sellvol", params.get("sell_quantity", 0)), 0))
        self._csv_path = str(params.get("csv_path") or self._default_csv_path())
        self._limit_ratio = float(params.get("limit_ratio", self._default_limit_ratio(self.stock_code)) or 0.10)
        self._book_volume_multiplier = float(params.get("book_volume_multiplier", 1.0) or 1.0)

        self._flag = self._to_int(params.get("flag"), 0)
        self._pre_bid = float(params.get("pre_bid", 0.0) or 0.0)
        self._open_flag = self._to_int(params.get("open_flag"), 0)
        self._timestamp_2: Optional[datetime] = self._parse_dt(params.get("timestamp_2"))
        self._timestamp_10: Optional[datetime] = self._parse_dt(params.get("timestamp_10"))
        self._up_sell = self._to_int(params.get("up_sell"), 0)
        self._pre_close = float(params.get("pre_close", 0.0) or 0.0)
        self._pre_flag = self._to_int(params.get("pre_flag"), self._flag)
        self._pre_up_amount = float(params.get("pre_up_amt", params.get("pre_up_amount", 0.0)) or 0.0)
        self._limit_up_price = float(params.get("limit_up_price", params.get("up_pr", 0.0)) or 0.0)
        self._limit_down_price = float(params.get("limit_down_price", params.get("lo_pr", 0.0)) or 0.0)
        self._submitted_actions = set(str(item) for item in (params.get("submitted_actions") or []))

    # ------------------------------------------------------------------ Selection

    def select_stocks(self) -> List[StrategyConfig]:
        """Read Juejin's ``sell_10.csv`` and create one strategy per row."""
        csv_path = Path(self.config.params.get("csv_path") or self._default_csv_path())
        if not csv_path.exists():
            logger.warning("JuejinSellStrategy: CSV 文件不存在，跳过: %s", csv_path)
            return []

        configs: List[StrategyConfig] = []
        with csv_path.open("r", encoding="utf-8-sig", newline="") as fp:
            reader = csv.DictReader(fp)
            for row_no, row in enumerate(reader, start=2):
                stock_code = self._normalize_stock_code(row.get("symbol") or row.get("股票代码") or "")
                sell_quantity = max(0, self._to_int(row.get("sellvol", row.get("卖出数量", 0)), 0))
                if not stock_code or sell_quantity <= 0:
                    logger.warning("JuejinSellStrategy: 第 %d 行配置无效，已跳过: %s", row_no, row)
                    continue

                configs.append(
                    StrategyConfig(
                        stock_code=stock_code,
                        params={
                            "csv_path": str(csv_path),
                            "source_row": row_no,
                            "source_symbol": str(row.get("symbol", "") or ""),
                            "exp": self._to_int(row.get("exp"), 0),
                            "sellvol": sell_quantity,
                            "nick": str(row.get("nick", "") or stock_code),
                            "instance_key": f"juejin_sell:{stock_code}",
                        },
                    )
                )
        return configs

    # ------------------------------------------------------------------ Tick handling

    def process_tick(self, tick: TickData) -> None:
        """Process one tick without BaseStrategy's active-exit short circuit.

        The Juejin strategy can cancel/reprice active sell orders in reaction to
        new ticks, so the generic "skip while a sell order is active" guard would
        suppress intended behaviour.  Orders still go through ``TradeExecutor``.
        """
        if self.status not in (StrategyStatus.RUNNING,):
            return
        if tick.stock_code != self.stock_code:
            return
        try:
            if self._position_mgr:
                self._position_mgr.update_price(self.stock_code, tick.last_price)
            if self._process_pending_close_request():
                return
            self.on_tick(tick)
        except Exception as exc:
            logger.error("Strategy[%s] JuejinSellStrategy tick 异常: %s", self.strategy_id[:8], exc, exc_info=True)
            self.status = StrategyStatus.ERROR

    def on_tick(self, tick: TickData) -> Optional[dict]:
        now = self._tick_time(tick)
        if now.time() < time(9, 15):
            return None

        pre_close = self._resolve_pre_close(tick)
        if pre_close <= 0:
            return None
        limit_up, limit_down = self._resolve_limit_prices(pre_close)

        bid_p = float(tick.bid1 or tick.last_price or 0.0)
        ask_p = float(tick.ask1 or tick.last_price or 0.0)
        bid_v = int(tick.bid_volumes[0] if tick.bid_volumes else 0)
        ask_v = int(tick.ask_volumes[0] if tick.ask_volumes else 0)
        if bid_p <= 0 and ask_p <= 0:
            return None

        open_price = float(tick.open or tick.last_price or 0.0)
        high_price = float(tick.high or tick.last_price or 0.0)
        low_price = float(tick.low or tick.last_price or 0.0)
        position = self._get_position()

        if (
            now.hour == 9
            and now.minute == 24
            and now.second >= 56
            and bid_p > pre_close * 0.94
            and bid_p < pre_close * 0.995
            and self._exp == 1
            and position is not None
            and self._flag == 0
        ):
            first_qty = min(self._target_sell_quantity(position), self._position_available(position))
            first_order = self._submit_sell(
                first_qty,
                limit_down,
                "竞价严重不及预期: 第一笔跌停价卖出",
                action_key="auction_under_expectation_primary",
            )
            remaining_qty = max(0, self._position_available(position) - first_qty)
            self._submit_sell(
                remaining_qty,
                round(pre_close * 1.05, 2),
                "竞价严重不及预期: 剩余挂 5% 反弹价",
                action_key="auction_under_expectation_rebound",
            )
            if first_order or remaining_qty > 0:
                self._flag = 1
                self._log_status("竞价严重不及预期")

        if now.time() < time(9, 26) or now.time() > time(14, 57):
            return None

        if (
            position is not None
            and self._exp == 1
            and bid_p > pre_close * 0.94
            and bid_p < pre_close * 0.98
            and now.time() < time(9, 31)
        ):
            self._cancel_active_orders("严重不及预期前撤单")
            order = self._submit_sell(
                self._target_sell_quantity(position),
                round(bid_p * 0.99, 2),
                "严重不及预期卖出",
                action_key="early_under_expectation",
            )
            if order:
                self._exp = -1
                self._log_status("严重不及预期卖出")

        if self._price_equal(bid_p, limit_up) and self._flag != 10:
            self._timestamp_2 = now
        if self._price_equal(bid_p, limit_up) and self._book_amount(bid_p, bid_v) > 120_000_000 and self._flag != 99:
            self._flag = 10
            self._open_flag = 0
        if bid_p < pre_close * 0.975 and self._flag not in (99, -3, 66):
            self._flag = -3

        if (
            self._flag == -3
            and self._pre_bid >= pre_close * 1.02
            and bid_p < pre_close * 1.02
            and low_price < pre_close * 0.97
            and now.time() > time(9, 33)
            and position is not None
        ):
            self._cancel_active_orders("-3 反弹失败前撤单")
            first_qty = self._target_sell_quantity(position)
            first_order = self._submit_sell(
                first_qty,
                round(bid_p * 0.99, 2),
                "-3 反弹失败试错离场",
                action_key="weak_rebound_primary",
            )
            remaining_qty = max(0, self._position_available(position) - min(first_qty, self._position_available(position)))
            self._submit_sell(
                remaining_qty,
                round(pre_close * 1.04, 2),
                "-3 反弹失败剩余挂 4%",
                action_key="weak_rebound_remainder",
            )
            if first_order:
                self._flag = 0
                self._log_status("-3 反弹失败")

        if (
            self._price_equal(ask_p, limit_down)
            and open_price > pre_close * 0.91
            and self._flag != 99
            and position is not None
            and self._book_amount(ask_p, ask_v) > 50_000_000
        ):
            self._cancel_active_orders("跌停止损清仓前撤单")
            order = self._submit_sell(
                self._position_available(position),
                limit_down,
                "非跌停开后跌停止损清仓",
                action_key="limit_down_clear",
            )
            if order:
                self._flag = 99
                self._log_status("跌停止损清仓")

        if self._flag == 5 and bid_p < pre_close * 1.05:
            if now.time() < time(9, 31) and open_price < pre_close * 1.05:
                logger.info("JuejinSellStrategy[%s] %s 第一分钟跌破 5%%，暂不卖", self.strategy_id[:8], self._nick)
            elif position is not None and high_price > pre_close * 1.065:
                self._cancel_active_orders("高点超过 6.5% 后跌破 5%")
                order = self._submit_sell(
                    self._target_sell_quantity(position),
                    round(bid_p * 0.99, 2),
                    "高点超过 6.5% 后跌破 5%",
                    action_key="flag5_high_pullback",
                )
                if order:
                    self._flag = -9
                    self._log_status("flag 5 跌破 5%")
            elif position is not None and bid_p < pre_close * 1.03:
                self._cancel_active_orders("flag 5 跌破 3%")
                order = self._submit_sell(
                    self._target_sell_quantity(position),
                    round(bid_p * 0.99, 2),
                    "flag 5 跌破 3%",
                    action_key="flag5_break3",
                )
                if order:
                    self._flag = -9
                    self._log_status("flag 5 跌破 3%")

        if high_price > pre_close * 1.085 and bid_p < pre_close * 1.07 and self._flag == 7 and position is not None:
            self._cancel_active_orders("冲高 8.5% 未封板跌破 7%")
            order = self._submit_sell(
                self._target_sell_quantity(position),
                round(bid_p * 0.985, 2) + 0.01,
                "冲高 8.5% 未封板跌破 7%",
                action_key="flag7_break7_after_high",
            )
            if order:
                self._flag = -9
                self._log_status("flag 7 跌破 7%")

        if bid_p < pre_close * 1.065 and self._flag == 7 and now.time() < time(9, 31) and position is not None:
            self._cancel_active_orders("早盘到 7% 后跌破 6.5%")
            order = self._submit_sell(
                self._target_sell_quantity(position),
                round(bid_p * 0.99, 2),
                "早盘到 7% 后跌破 6.5%",
                action_key="flag7_morning_take_profit",
            )
            if order:
                self._flag = -9
                self._log_status("flag 7 早盘回落")

        if self._flag == 10 and self._open_flag != 10:
            gap_seconds = self._seconds_since(self._timestamp_2, now)
            bid_amount = self._book_amount(bid_p, bid_v)
            opened_or_weakened = (
                bid_p < limit_up
                or (
                    self._price_equal(bid_p, limit_up)
                    and bid_amount < 100_000_000
                    and self._pre_up_amount - bid_amount > 25_000_000
                )
                or (
                    self._price_equal(bid_p, limit_up)
                    and bid_amount < 160_000_000
                    and self._pre_up_amount - bid_amount > 80_000_000
                )
            )
            if opened_or_weakened:
                if gap_seconds < 60 and open_price < limit_up and now.time() < time(9, 40):
                    logger.info("JuejinSellStrategy[%s] %s 封板 1 分钟内开板，暂不卖", self.strategy_id[:8], self._nick)
                elif self._up_sell != 99 and position is not None:
                    order = self._submit_sell(
                        self._target_sell_quantity(position),
                        round(bid_p * 0.982, 2),
                        "涨停开板先卖一笔",
                        action_key="limit_open_first_sell",
                    )
                    if order:
                        self._up_sell = 99
                        self._log_status("涨停开板先卖一笔")
            if bid_p < limit_up:
                self._timestamp_10 = now
                self._open_flag = 10
                if self._up_sell == 99:
                    self._flag = -9

        if self._open_flag == 10 and bid_p < limit_up:
            gap_seconds = self._seconds_since(self._timestamp_10, now)
            if gap_seconds > 300 and position is not None:
                order = self._submit_sell(
                    self._target_sell_quantity(position),
                    round(bid_p, 2),
                    "涨停开板 5 分钟不回封卖出",
                    action_key="limit_open_5min",
                )
                if order:
                    self._flag = 0
                    self._open_flag = 0
                    self._timestamp_10 = now
                    self._log_status("涨停开板 5 分钟不回封")

        if bid_p > pre_close * 1.07 and bid_p < limit_up and self._flag >= 0 and self._flag != 10:
            self._flag = 7
        if bid_p > pre_close * 1.04 and bid_p < pre_close * 1.07 and self._flag >= -3:
            self._flag = 5

        self._pre_bid = bid_p
        if self._price_equal(bid_p, limit_up):
            self._pre_up_amount = self._book_amount(bid_p, bid_v)
        if self._flag != self._pre_flag:
            logger.info(
                "JuejinSellStrategy[%s] %s status %s -> %s",
                self.strategy_id[:8],
                self._nick,
                self._pre_flag,
                self._flag,
            )
            self._pre_flag = self._flag
            self.request_state_persist(reason=f"juejin_sell_flag:{self.strategy_id}", min_interval_sec=1.0)
        return None

    # ------------------------------------------------------------------ Account recovery hooks

    def can_recover_from_account_position(self, account_position) -> bool:
        code = self._normalize_stock_code(self._get_attr(account_position, "stock_code", ""))
        volume = self._to_int(self._get_attr(account_position, "volume", 0), 0)
        return code == self.stock_code and volume > 0

    def suggest_account_recovery_quantity(self, account_position) -> int:
        if not self.can_recover_from_account_position(account_position):
            return 0
        return max(0, self._to_int(self._get_attr(account_position, "volume", 0), 0))

    def on_account_position_recovered(self, position: PositionInfo, trade_day: str) -> None:
        logger.info(
            "JuejinSellStrategy[%s] 接管账户持仓 stock=%s qty=%d available=%d trade_day=%s",
            self.strategy_id[:8],
            position.stock_code,
            int(position.total_quantity or 0),
            int(position.available_quantity or 0),
            trade_day,
        )

    # ------------------------------------------------------------------ Persistence

    def _get_custom_state(self) -> dict:
        return {
            "source_symbol": self._source_symbol,
            "nick": self._nick,
            "exp": self._exp,
            "sellvol": self._sell_quantity,
            "csv_path": self._csv_path,
            "limit_ratio": self._limit_ratio,
            "book_volume_multiplier": self._book_volume_multiplier,
            "flag": self._flag,
            "pre_bid": self._pre_bid,
            "open_flag": self._open_flag,
            "timestamp_2": self._format_dt(self._timestamp_2),
            "timestamp_10": self._format_dt(self._timestamp_10),
            "up_sell": self._up_sell,
            "pre_close": self._pre_close,
            "pre_flag": self._pre_flag,
            "pre_up_amount": self._pre_up_amount,
            "limit_up_price": self._limit_up_price,
            "limit_down_price": self._limit_down_price,
            "submitted_actions": sorted(self._submitted_actions),
        }

    def _restore_custom_state(self, state: dict) -> None:
        payload = dict(state or {})
        self._source_symbol = str(payload.get("source_symbol", self._source_symbol) or "")
        self._nick = str(payload.get("nick", self._nick) or self.stock_code)
        self._exp = self._to_int(payload.get("exp"), self._exp)
        self._sell_quantity = max(0, self._to_int(payload.get("sellvol"), self._sell_quantity))
        self._csv_path = str(payload.get("csv_path", self._csv_path) or self._default_csv_path())
        self._limit_ratio = float(payload.get("limit_ratio", self._limit_ratio) or self._limit_ratio)
        self._book_volume_multiplier = float(
            payload.get("book_volume_multiplier", self._book_volume_multiplier) or self._book_volume_multiplier
        )
        self._flag = self._to_int(payload.get("flag"), self._flag)
        self._pre_bid = float(payload.get("pre_bid", self._pre_bid) or 0.0)
        self._open_flag = self._to_int(payload.get("open_flag"), self._open_flag)
        self._timestamp_2 = self._parse_dt(payload.get("timestamp_2"))
        self._timestamp_10 = self._parse_dt(payload.get("timestamp_10"))
        self._up_sell = self._to_int(payload.get("up_sell"), self._up_sell)
        self._pre_close = float(payload.get("pre_close", self._pre_close) or 0.0)
        self._pre_flag = self._to_int(payload.get("pre_flag"), self._pre_flag)
        self._pre_up_amount = float(payload.get("pre_up_amount", self._pre_up_amount) or 0.0)
        self._limit_up_price = float(payload.get("limit_up_price", self._limit_up_price) or 0.0)
        self._limit_down_price = float(payload.get("limit_down_price", self._limit_down_price) or 0.0)
        self._submitted_actions = set(str(item) for item in (payload.get("submitted_actions") or []))

    # ------------------------------------------------------------------ Helpers

    def _submit_sell(self, quantity: int, price: float, remark: str, action_key: str = "") -> Optional[Order]:
        if action_key and action_key in self._submitted_actions:
            return None
        if not self._trade_executor:
            return None

        position = self._get_position()
        available = self._position_available(position)
        sell_quantity = min(max(0, int(quantity or 0)), available)
        if sell_quantity <= 0 or price <= 0:
            return None

        order = self._trade_executor.sell_limit(
            self.strategy_id,
            self.strategy_name,
            self.stock_code,
            float(price),
            sell_quantity,
            remark,
        )
        self._track_order(order)
        if action_key:
            self._submitted_actions.add(action_key)
        self.__class__._sync_class_stats(self._position_mgr)
        logger.info(
            "JuejinSellStrategy[%s] 卖单提交 stock=%s nick=%s qty=%d price=%.3f reason=%s",
            self.strategy_id[:8],
            self.stock_code,
            self._nick,
            sell_quantity,
            float(price),
            remark,
        )
        self.request_state_persist(reason=f"juejin_sell_order:{self.strategy_id}", min_interval_sec=0.0)
        return order

    def _cancel_active_orders(self, remark: str) -> int:
        if not self._trade_executor:
            return 0
        cancel_order = getattr(self._trade_executor, "cancel_order", None)
        if not callable(cancel_order):
            return 0

        canceled = 0
        for order_uuid, order in list(self._pending_orders.items()):
            if not order.is_active():
                continue
            if order.status in (OrderStatus.REPORTED_CANCEL, OrderStatus.PARTSUCC_CANCEL):
                continue
            if bool(cancel_order(order_uuid, remark=remark)):
                if order.is_active():
                    order.status = OrderStatus.CANCELED
                self._pending_orders.pop(order_uuid, None)
                canceled += 1
        if canceled:
            logger.info("JuejinSellStrategy[%s] 撤单 %d 笔 reason=%s", self.strategy_id[:8], canceled, remark)
        return canceled

    def _get_position(self) -> Optional[PositionInfo]:
        if not self._position_mgr:
            return None
        return self._position_mgr.get_position(self.strategy_id)

    def _position_available(self, position: Optional[PositionInfo]) -> int:
        if not position:
            return 0
        total = max(0, int(getattr(position, "total_quantity", 0) or 0))
        available = int(getattr(position, "available_quantity", 0) or 0)
        if available <= 0:
            available = int(getattr(position, "sellable_base_quantity", 0) or 0)
        if available <= 0:
            return 0
        return min(total, max(0, available))

    def _target_sell_quantity(self, position: Optional[PositionInfo]) -> int:
        available = self._position_available(position)
        if self._sell_quantity <= 0:
            return available
        return min(self._sell_quantity, available)

    def _resolve_pre_close(self, tick: TickData) -> float:
        value = float(tick.pre_close or 0.0)
        if value > 0:
            self._pre_close = value
        return float(self._pre_close or 0.0)

    def _resolve_limit_prices(self, pre_close: float) -> tuple[float, float]:
        if self._limit_up_price <= 0:
            self._limit_up_price = round(pre_close * (1 + self._limit_ratio), 2)
        if self._limit_down_price <= 0:
            self._limit_down_price = round(pre_close * (1 - self._limit_ratio), 2)
        return self._limit_up_price, self._limit_down_price

    def _book_amount(self, price: float, volume: int) -> float:
        return float(price or 0.0) * float(volume or 0) * self._book_volume_multiplier

    def _log_status(self, reason: str) -> None:
        logger.info(
            "JuejinSellStrategy[%s] %s stock=%s status=%s reason=%s",
            self.strategy_id[:8],
            self._nick,
            self.stock_code,
            self._flag,
            reason,
        )

    @staticmethod
    def _tick_time(tick: TickData) -> datetime:
        return tick.data_time or tick.recv_time or datetime.now()

    @staticmethod
    def _seconds_since(start: Optional[datetime], now: datetime) -> float:
        if not start:
            return 0.0
        return max(0.0, (now - start).total_seconds())

    @staticmethod
    def _price_equal(left: float, right: float) -> bool:
        if left <= 0 or right <= 0:
            return False
        return abs(float(left) - float(right)) <= 0.005

    @staticmethod
    def _format_dt(value: Optional[datetime]) -> str:
        return value.isoformat() if isinstance(value, datetime) else ""

    @staticmethod
    def _parse_dt(value) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    @staticmethod
    def _default_csv_path() -> Path:
        return Path(__file__).resolve().parents[2] / "docs" / "掘金" / "sell_10.csv"

    @staticmethod
    def _default_limit_ratio(stock_code: str) -> float:
        code = str(stock_code or "").strip()
        if code.startswith(("300", "301", "688", "689")):
            return 0.20
        if code.startswith(("4", "8", "9")):
            return 0.30
        return 0.10

    @staticmethod
    def _normalize_stock_code(value: Any) -> str:
        text = str(value or "").strip().upper()
        if not text:
            return ""
        if text.startswith(("SZSE.", "SHSE.", "BJSE.")):
            text = text.split(".", 1)[1]
        elif "." in text:
            left, right = text.split(".", 1)
            if len(left) == 6 and left.isdigit():
                text = left
            elif len(right) == 6 and right.isdigit():
                text = right
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) == 6:
            return digits
        if 0 < len(digits) < 6:
            return digits.zfill(6)
        return ""

    @staticmethod
    def _to_int(value, default: int = 0) -> int:
        try:
            if value in (None, ""):
                return int(default)
            return int(float(value))
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _get_attr(payload, name: str, default=None):
        if isinstance(payload, dict):
            return payload.get(name, default)
        return getattr(payload, name, default)


__all__ = ["JuejinSellStrategy"]
