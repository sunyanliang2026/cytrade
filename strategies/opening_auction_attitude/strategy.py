"""Observe-only opening auction attitude strategy."""
from __future__ import annotations

from datetime import datetime, time
from typing import Any

from core.l2_models import L2OrderEvent, L2OrderQueueEvent, L2QuoteEvent, L2TransactionEvent
from core.models import TickData
from strategy.base import BaseStrategy
from strategy.models import StrategyConfig
from strategies.opening_auction_attitude.models import (
    AuctionDecision,
    AuctionL1Window,
    AuctionL2Window,
    AuctionPricePoint,
    AuctionScoreConfig,
    OpenVerifyConfig,
    OpenVerifyDecision,
    OpenVerifyPoint,
    OpenVerifyWindow,
)
from strategies.opening_auction_attitude.score import evaluate_auction_attitude, evaluate_open_behavior


class OpeningAuctionAttitudeStrategy(BaseStrategy):
    """Collect opening-auction evidence and classify it without trading."""

    strategy_name = "OpeningAuctionAttitudeStrategy"

    def __init__(self, config: StrategyConfig, trade_executor=None, position_manager=None):
        super().__init__(config, trade_executor, position_manager)
        params = getattr(config, "params", {}) or {}
        default_score_config = AuctionScoreConfig()
        self._score_config = AuctionScoreConfig(
            min_low_to_final_lift_pct=float(
                params.get("min_low_to_final_lift_pct", default_score_config.min_low_to_final_lift_pct)
            ),
            min_final_gap_pct=float(params.get("min_final_gap_pct", default_score_config.min_final_gap_pct)),
            min_money_lift_ratio=float(params.get("min_money_lift_ratio", default_score_config.min_money_lift_ratio)),
            strong_money_lift_ratio=float(
                params.get("strong_money_lift_ratio", default_score_config.strong_money_lift_ratio)
            ),
            close_to_high_tolerance_pct=float(
                params.get("close_to_high_tolerance_pct", default_score_config.close_to_high_tolerance_pct)
            ),
            big_buy_ratio_threshold=float(
                params.get("big_buy_ratio_threshold", default_score_config.big_buy_ratio_threshold)
            ),
            sell_pressure_ratio_threshold=float(
                params.get("sell_pressure_ratio_threshold", default_score_config.sell_pressure_ratio_threshold)
            ),
        )
        default_open_config = OpenVerifyConfig()
        self._open_verify_config = OpenVerifyConfig(
            min_direct_pull_pct=float(params.get("min_direct_pull_pct", default_open_config.min_direct_pull_pct)),
            max_direct_drawdown_pct=float(
                params.get("max_direct_drawdown_pct", default_open_config.max_direct_drawdown_pct)
            ),
            min_wash_dip_pct=float(params.get("min_wash_dip_pct", default_open_config.min_wash_dip_pct)),
            max_wash_drawdown_pct=float(
                params.get("max_wash_drawdown_pct", default_open_config.max_wash_drawdown_pct)
            ),
            recover_open_tolerance_pct=float(
                params.get("recover_open_tolerance_pct", default_open_config.recover_open_tolerance_pct)
            ),
            min_rebreak_pct=float(params.get("min_rebreak_pct", default_open_config.min_rebreak_pct)),
            breakdown_pct=float(params.get("breakdown_pct", default_open_config.breakdown_pct)),
            failed_recover_tolerance_pct=float(
                params.get("failed_recover_tolerance_pct", default_open_config.failed_recover_tolerance_pct)
            ),
            min_buy_trade_ratio=float(params.get("min_buy_trade_ratio", default_open_config.min_buy_trade_ratio)),
            sell_pressure_ratio_threshold=float(
                params.get("open_sell_pressure_ratio_threshold", default_open_config.sell_pressure_ratio_threshold)
            ),
        )
        self._price_points: list[AuctionPricePoint] = []
        self._open_points: list[OpenVerifyPoint] = []
        self._pre_close: float = float(params.get("pre_close", 0.0) or 0.0)
        self._l2quote_count = 0
        self._l2order_count = 0
        self._l2transaction_count = 0
        self._l2orderqueue_count = 0
        self._big_buy_order_amount = 0.0
        self._big_sell_order_amount = 0.0
        self._cancel_buy_order_amount = 0.0
        self._cancel_sell_order_amount = 0.0
        self._big_buy_trade_amount = 0.0
        self._big_sell_trade_amount = 0.0
        self._orders_by_no: dict[str, dict[str, Any]] = {}
        self._final_tx_amount = 0.0
        self._final_tx_count = 0
        self._final_from_last20_bid_amount = 0.0
        self._final_from_last10_bid_amount = 0.0
        self._final_from_limit_up_bid_amount = 0.0
        self._last20_bid_amount = 0.0
        self._last10_bid_amount = 0.0
        self._last20_bid_count = 0
        self._last10_bid_count = 0
        self._final_auction_quote_amount = 0.0
        self._open_price_0925 = 0.0
        self._post_0920_low_price = 0.0
        self._post_0920_low_time: datetime | None = None
        self._limit_up_price = float(params.get("limit_up_price", 0.0) or 0.0)
        self._open_l2transaction_count = 0
        self._open_buy_trade_amount = 0.0
        self._open_sell_trade_amount = 0.0
        self._last_decision: AuctionDecision | None = None
        self._last_open_decision: OpenVerifyDecision | None = None
        self._last_event_payload: dict[str, Any] | None = None
        self._quote_volume_unit = float(params.get("quote_volume_unit", 100) or 100)

    @classmethod
    def required_data_kinds(cls) -> set[str]:
        return {"tick", "l2quote", "l2transaction", "l2order", "l2orderqueue"}

    def on_tick(self, tick: TickData) -> None:
        if tick.stock_code != self.stock_code:
            return None
        event_time = tick.data_time or tick.recv_time
        price = float(tick.last_price or 0.0)
        self._record_post_0920_low(event_time=event_time, price=price)
        if self._in_auction_window(event_time):
            self._record_price_point(
                event_time=event_time,
                price=price,
                matched_amount=float(tick.amount or 0.0),
                pre_close=float(tick.pre_close or 0.0),
                amount_source="tick_amount" if float(tick.amount or 0.0) > 0 else "",
            )
        elif self._in_open_verify_window(event_time):
            self._record_open_point(
                event_time=event_time,
                price=float(tick.last_price or 0.0),
                amount=float(tick.amount or 0.0),
                volume=float(tick.volume or 0.0),
            )
        return None

    def select_stocks(self) -> list[StrategyConfig]:
        return []

    def on_l2_quote(self, event: L2QuoteEvent) -> None:
        if event.stock_code != self.stock_code:
            return
        if float(event.limit_up_price or 0.0) > 0:
            self._limit_up_price = float(event.limit_up_price)
        quote_amount = self._auction_quote_amount(event.raw_xt_fields, fallback_price=float(event.last_price or 0.0))
        price = quote_amount["price"] or float(event.last_price or 0.0)
        self._record_post_0920_low(event_time=event.event_time, price=price)
        if not self._in_auction_window(event.event_time):
            return
        self._l2quote_count += 1
        self._record_price_point(
            event_time=event.event_time,
            price=price,
            matched_amount=quote_amount["matched_amount"],
            pre_close=float(event.pre_close or 0.0),
            matched_volume=quote_amount["matched_volume"],
            unmatched_buy_volume=quote_amount["unmatched_buy_volume"],
            unmatched_sell_volume=quote_amount["unmatched_sell_volume"],
            unmatched_buy_amount=quote_amount["unmatched_buy_amount"],
            unmatched_sell_amount=quote_amount["unmatched_sell_amount"],
            amount_source=quote_amount["amount_source"],
        )
        if self._in_final_auction_result_window(event.event_time):
            raw_amount = self._amount_from_raw(event.raw_xt_fields)
            final_amount = raw_amount if raw_amount > 0 else float(quote_amount["matched_amount"] or 0.0)
            if final_amount >= self._final_auction_quote_amount:
                self._final_auction_quote_amount = final_amount
                self._open_price_0925 = float(price or 0.0)

    def on_l2_order(self, event: L2OrderEvent) -> None:
        if event.stock_code != self.stock_code or not self._in_auction_window(event.event_time):
            return
        self._l2order_count += 1
        amount = self._event_amount(event)
        side = str(event.side or "").strip().upper()
        is_cancel = bool(getattr(event, "is_cancel", False))
        entrust_no = str(getattr(event, "entrust_no", "") or "").strip()
        if entrust_no:
            self._orders_by_no[entrust_no] = {
                "side": side,
                "event_time": event.event_time,
                "price": float(event.price or 0.0),
                "amount": amount,
                "is_cancel": is_cancel,
            }
        if is_cancel:
            if side == "BUY":
                self._cancel_buy_order_amount += amount
            elif side == "SELL":
                self._cancel_sell_order_amount += amount
            return
        if side == "BUY":
            self._big_buy_order_amount += amount
            if self._in_last20_bid_window(event.event_time):
                self._last20_bid_amount += amount
                self._last20_bid_count += 1
            if self._in_last10_bid_window(event.event_time):
                self._last10_bid_amount += amount
                self._last10_bid_count += 1
        elif side == "SELL":
            self._big_sell_order_amount += amount
        elif side == "CANCEL_BUY":
            self._cancel_buy_order_amount += amount
        elif side == "CANCEL_SELL":
            self._cancel_sell_order_amount += amount

    def on_l2_transaction(self, event: L2TransactionEvent) -> None:
        if event.stock_code != self.stock_code:
            return
        side = self._trade_side(event)
        amount = self._event_amount(event)
        if self._in_auction_window(event.event_time):
            self._l2transaction_count += 1
            if amount > 0 and float(event.price or 0.0) > 0:
                self._final_tx_amount += amount
                self._final_tx_count += 1
                buy_order = self._orders_by_no.get(str(event.buy_no or "").strip())
                if buy_order:
                    order_time = buy_order.get("event_time")
                    if self._in_last20_bid_window(order_time):
                        self._final_from_last20_bid_amount += amount
                    if self._in_last10_bid_window(order_time):
                        self._final_from_last10_bid_amount += amount
                    if self._is_limit_up_bid_order(buy_order):
                        self._final_from_limit_up_bid_amount += amount
            if side == "BUY":
                self._big_buy_trade_amount += amount
            elif side == "SELL":
                self._big_sell_trade_amount += amount
        elif self._in_open_verify_window(event.event_time):
            self._open_l2transaction_count += 1
            if side == "BUY":
                self._open_buy_trade_amount += amount
            elif side == "SELL":
                self._open_sell_trade_amount += amount

    def on_l2_orderqueue(self, event: L2OrderQueueEvent) -> None:
        if event.stock_code != self.stock_code or not self._in_auction_window(event.event_time):
            return
        self._l2orderqueue_count += 1

    def classify_auction(self) -> AuctionDecision:
        decision = evaluate_auction_attitude(
            self.build_l1_window(),
            self.build_l2_window(),
            self._score_config,
        )
        self._last_decision = decision
        self._last_event_payload = self.build_event_payload(decision)
        return decision

    def verify_open_behavior(self, decision: AuctionDecision | None = None) -> OpenVerifyDecision:
        decision = decision or self._last_decision or self.classify_auction()
        open_decision = evaluate_open_behavior(
            self.build_open_verify_window(decision),
            self._open_verify_config,
        )
        self._last_open_decision = open_decision
        return open_decision

    def build_l1_window(self) -> AuctionL1Window:
        return AuctionL1Window(
            symbol=self.stock_code,
            pre_close=float(self._pre_close or 0.0),
            points=list(self._price_points),
        )

    def build_l2_window(self) -> AuctionL2Window:
        return AuctionL2Window(
            symbol=self.stock_code,
            l2quote_count=self._l2quote_count,
            l2order_count=self._l2order_count,
            l2transaction_count=self._l2transaction_count,
            l2orderqueue_count=self._l2orderqueue_count,
            big_buy_order_amount=self._big_buy_order_amount,
            big_sell_order_amount=self._big_sell_order_amount,
            cancel_buy_order_amount=self._cancel_buy_order_amount,
            cancel_sell_order_amount=self._cancel_sell_order_amount,
            big_buy_trade_amount=self._big_buy_trade_amount,
            big_sell_trade_amount=self._big_sell_trade_amount,
        )

    def build_open_verify_window(self, decision: AuctionDecision | None = None) -> OpenVerifyWindow:
        decision = decision or self._last_decision
        evidence = dict(getattr(decision, "evidence", {}) or {})
        auction_final_price = float(evidence.get("auction_final_price", 0.0) or 0.0)
        if auction_final_price <= 0 and self._price_points:
            auction_points = [
                point
                for point in self._price_points
                if point.event_time is None or self._in_auction_window(point.event_time)
            ]
            if auction_points:
                auction_points.sort(key=lambda item: (item.event_time is None, item.event_time or datetime.min))
                auction_final_price = float(auction_points[-1].price or 0.0)
        return OpenVerifyWindow(
            symbol=self.stock_code,
            auction_label=str(getattr(decision, "auction_label", "") or ""),
            auction_final_price=auction_final_price,
            points=list(self._open_points),
            buy_trade_amount=self._open_buy_trade_amount,
            sell_trade_amount=self._open_sell_trade_amount,
        )

    def build_event_payload(self, decision: AuctionDecision | None = None) -> dict[str, Any]:
        decision = decision or self._last_decision or self.classify_auction()
        open_decision = self.verify_open_behavior(decision)
        evidence = dict(decision.evidence or {})
        open_evidence = dict(open_decision.evidence or {})
        return {
            "event_name": "MSF_AUCTION_ATTITUDE",
            "strategy": self.strategy_name,
            "strategy_id": self.strategy_id,
            "symbol": self.stock_code,
            "auction_label": decision.auction_label,
            "auction_speed_score": decision.auction_speed_score,
            "auction_attitude_score": decision.auction_attitude_score,
            "reason": decision.reason,
            "open_verify_path": open_decision.open_verify_path,
            "open_verify_score": open_decision.open_verify_score,
            "open_verify_reason": open_decision.reason,
            "price_point_count": len(self._price_points),
            "open_point_count": len(self._open_points),
            "l2quote_count": self._l2quote_count,
            "l2order_count": self._l2order_count,
            "l2transaction_count": self._l2transaction_count,
            "l2orderqueue_count": self._l2orderqueue_count,
            "open_l2transaction_count": self._open_l2transaction_count,
            "open_buy_trade_amount": self._open_buy_trade_amount,
            "open_sell_trade_amount": self._open_sell_trade_amount,
            "auction_reference": self.build_auction_reference_metrics(),
            "evidence": evidence,
            "open_evidence": open_evidence,
        }

    def get_last_event_payload(self) -> dict[str, Any] | None:
        return dict(self._last_event_payload) if self._last_event_payload else None

    def _record_price_point(
        self,
        *,
        event_time: datetime | None,
        price: float,
        matched_amount: float,
        pre_close: float,
        matched_volume: float = 0.0,
        unmatched_buy_volume: float = 0.0,
        unmatched_sell_volume: float = 0.0,
        unmatched_buy_amount: float = 0.0,
        unmatched_sell_amount: float = 0.0,
        amount_source: str = "",
    ) -> None:
        if price <= 0:
            return
        if pre_close > 0:
            self._pre_close = pre_close
        self._price_points.append(
            AuctionPricePoint(
                event_time=event_time,
                price=price,
                matched_amount=max(0.0, float(matched_amount or 0.0)),
                matched_volume=max(0.0, float(matched_volume or 0.0)),
                unmatched_buy_volume=max(0.0, float(unmatched_buy_volume or 0.0)),
                unmatched_sell_volume=max(0.0, float(unmatched_sell_volume or 0.0)),
                unmatched_buy_amount=max(0.0, float(unmatched_buy_amount or 0.0)),
                unmatched_sell_amount=max(0.0, float(unmatched_sell_amount or 0.0)),
                amount_source=str(amount_source or ""),
            )
        )

    def _record_open_point(
        self,
        *,
        event_time: datetime | None,
        price: float,
        amount: float = 0.0,
        volume: float = 0.0,
    ) -> None:
        if price <= 0:
            return
        self._open_points.append(
            OpenVerifyPoint(
                event_time=event_time,
                price=price,
                amount=max(0.0, float(amount or 0.0)),
                volume=max(0.0, float(volume or 0.0)),
            )
        )

    def _in_auction_window(self, event_time: datetime | None) -> bool:
        if event_time is None:
            return True
        clock = event_time.time()
        return self._score_config.window_start <= clock <= self._score_config.window_end

    def build_auction_reference_metrics(self) -> dict[str, Any]:
        final_amount = self._final_auction_quote_amount or self._final_tx_amount
        open_pct = 0.0
        if self._pre_close > 0 and self._open_price_0925 > 0:
            open_pct = (self._open_price_0925 / self._pre_close - 1.0) * 100.0
        final_vs_low_pct = 0.0
        if self._post_0920_low_price > 0 and self._open_price_0925 > 0:
            final_vs_low_pct = (self._open_price_0925 / self._post_0920_low_price - 1.0) * 100.0
        tx_detail_available = self._final_tx_amount > 0 and self._final_tx_count > 0
        if self._final_auction_quote_amount > 0:
            final_amount_source = "quote_0925"
        elif self._final_tx_amount > 0:
            final_amount_source = "l2transaction_sum"
        else:
            final_amount_source = ""
        return {
            "pre_close": float(self._pre_close or 0.0),
            "open_price_0925": float(self._open_price_0925 or 0.0),
            "open_pct": open_pct,
            "post_0920_low_price": float(self._post_0920_low_price or 0.0),
            "post_0920_low_time": self._post_0920_low_time.strftime("%H:%M:%S") if self._post_0920_low_time else "",
            "final_vs_post_0920_low_pct": final_vs_low_pct,
            "final_auction_amount": float(final_amount or 0.0),
            "final_amount_gt_3000w": final_amount > 30_000_000,
            "final_price_gt_post_0920_low": (
                self._post_0920_low_price > 0 and self._open_price_0925 > self._post_0920_low_price
            ),
            "open_pct_gt_3": open_pct > 3.0,
            "final_amount_source": final_amount_source,
            "tx_detail_available": tx_detail_available,
            "final_tx_amount": float(self._final_tx_amount or 0.0),
            "final_tx_count": int(self._final_tx_count or 0),
            "last10_bid_amount": float(self._last10_bid_amount or 0.0),
            "last20_bid_amount": float(self._last20_bid_amount or 0.0),
            "last10_bid_count": int(self._last10_bid_count or 0),
            "last20_bid_count": int(self._last20_bid_count or 0),
            "final_from_last20_bid_amount": float(self._final_from_last20_bid_amount or 0.0),
            "final_from_last20_bid_pct": self._amount_pct(self._final_from_last20_bid_amount, self._final_tx_amount),
            "final_from_last10_bid_amount": float(self._final_from_last10_bid_amount or 0.0),
            "final_from_last10_bid_pct": self._amount_pct(self._final_from_last10_bid_amount, self._final_tx_amount),
            "limit_up_price": float(self._effective_limit_up_price() or 0.0),
            "final_from_limit_up_bid_amount": float(self._final_from_limit_up_bid_amount or 0.0),
            "final_from_limit_up_bid_pct": self._amount_pct(self._final_from_limit_up_bid_amount, self._final_tx_amount),
        }

    def _in_final_auction_result_window(self, event_time: datetime | None) -> bool:
        if event_time is None:
            return False
        return time(9, 25, 0) <= event_time.time() <= self._score_config.window_end

    @staticmethod
    def _in_post_0920_reference_window(event_time: datetime | None) -> bool:
        if event_time is None:
            return False
        return time(9, 20, 0) <= event_time.time() <= time(9, 25, 5)

    def _record_post_0920_low(self, *, event_time: datetime | None, price: float) -> None:
        if not self._in_post_0920_reference_window(event_time):
            return
        price = float(price or 0.0)
        if price <= 0:
            return
        if self._post_0920_low_price <= 0 or price < self._post_0920_low_price:
            self._post_0920_low_price = price
            self._post_0920_low_time = event_time

    @staticmethod
    def _in_last20_bid_window(event_time: datetime | None) -> bool:
        if event_time is None:
            return False
        return time(9, 24, 40) <= event_time.time() < time(9, 25, 0)

    @staticmethod
    def _in_last10_bid_window(event_time: datetime | None) -> bool:
        if event_time is None:
            return False
        return time(9, 24, 50) <= event_time.time() < time(9, 25, 0)

    @staticmethod
    def _amount_pct(numerator: float, denominator: float) -> float:
        denominator = float(denominator or 0.0)
        if denominator <= 0:
            return 0.0
        return float(numerator or 0.0) / denominator * 100.0

    def _is_limit_up_bid_order(self, order: dict[str, Any]) -> bool:
        if str(order.get("side") or "").upper() != "BUY":
            return False
        limit_price = self._effective_limit_up_price()
        price = float(order.get("price", 0.0) or 0.0)
        return limit_price > 0 and price >= limit_price - 1e-8

    def _effective_limit_up_price(self) -> float:
        if self._limit_up_price > 0:
            return self._limit_up_price
        if self._pre_close <= 0:
            return 0.0
        if self.stock_code.startswith(("300", "301", "688", "689")):
            ratio = 0.20
        elif self.stock_code.startswith(("4", "8")):
            ratio = 0.30
        else:
            ratio = 0.10
        return round(self._pre_close * (1.0 + ratio) + 1e-9, 2)

    def _in_open_verify_window(self, event_time: datetime | None) -> bool:
        if event_time is None:
            return False
        clock = event_time.time()
        return self._open_verify_config.window_start <= clock <= self._open_verify_config.window_end

    @staticmethod
    def _event_amount(event: Any) -> float:
        amount = float(getattr(event, "amount", 0.0) or 0.0)
        if amount > 0:
            return amount
        return float(getattr(event, "price", 0.0) or 0.0) * float(getattr(event, "volume", 0.0) or 0.0)

    @staticmethod
    def _amount_from_raw(raw_fields: dict[str, Any] | None) -> float:
        raw_fields = raw_fields or {}
        for key in ("amount", "turnover", "matchAmount", "matchedAmount"):
            try:
                value = float(raw_fields.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value
        return 0.0

    def _auction_quote_amount(self, raw_fields: dict[str, Any] | None, *, fallback_price: float = 0.0) -> dict[str, Any]:
        raw_fields = raw_fields or {}
        bid_prices = self._list_values(raw_fields.get("bidPrice"))
        ask_prices = self._list_values(raw_fields.get("askPrice"))
        bid_volumes = self._list_values(raw_fields.get("bidVol"))
        ask_volumes = self._list_values(raw_fields.get("askVol"))
        bid0 = bid_prices[0] if bid_prices else 0.0
        ask0 = ask_prices[0] if ask_prices else 0.0
        price = bid0 or ask0 or fallback_price
        is_auction_book = bid0 > 0 and ask0 > 0 and abs(bid0 - ask0) < 1e-8
        if bid0 > 0 and ask0 > 0:
            price = bid0 if is_auction_book else fallback_price or bid0 or ask0

        bid_vol0 = bid_volumes[0] if bid_volumes else 0.0
        ask_vol0 = ask_volumes[0] if ask_volumes else 0.0
        if is_auction_book:
            matched_volume = min(bid_vol0, ask_vol0) if bid_vol0 > 0 and ask_vol0 > 0 else max(bid_vol0, ask_vol0)
            unmatched_buy_volume = bid_volumes[1] if len(bid_volumes) > 1 else 0.0
            unmatched_sell_volume = ask_volumes[1] if len(ask_volumes) > 1 else 0.0
        else:
            matched_volume = 0.0
            unmatched_buy_volume = 0.0
            unmatched_sell_volume = 0.0

        if is_auction_book and price > 0 and matched_volume > 0:
            return {
                "price": price,
                "matched_volume": matched_volume,
                "unmatched_buy_volume": unmatched_buy_volume,
                "unmatched_sell_volume": unmatched_sell_volume,
                "matched_amount": price * matched_volume * self._quote_volume_unit,
                "unmatched_buy_amount": price * unmatched_buy_volume * self._quote_volume_unit,
                "unmatched_sell_amount": price * unmatched_sell_volume * self._quote_volume_unit,
                "amount_source": "auction_book",
            }

        amount = self._amount_from_raw(raw_fields)
        raw_volume = self._raw_volume(raw_fields)
        return {
            "price": price,
            "matched_volume": raw_volume,
            "unmatched_buy_volume": unmatched_buy_volume,
            "unmatched_sell_volume": unmatched_sell_volume,
            "matched_amount": amount,
            "unmatched_buy_amount": price * unmatched_buy_volume * self._quote_volume_unit if price > 0 else 0.0,
            "unmatched_sell_amount": price * unmatched_sell_volume * self._quote_volume_unit if price > 0 else 0.0,
            "amount_source": "raw_amount" if amount > 0 else "",
        }

    @staticmethod
    def _list_values(value: Any) -> list[float]:
        if not isinstance(value, (list, tuple)):
            return []
        result = []
        for item in value:
            try:
                result.append(float(item or 0.0))
            except (TypeError, ValueError):
                result.append(0.0)
        return result

    @staticmethod
    def _raw_volume(raw_fields: dict[str, Any]) -> float:
        for key in ("pvolume", "volume"):
            try:
                value = float(raw_fields.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value
        return 0.0

    @staticmethod
    def _trade_side(event: L2TransactionEvent) -> str:
        side = str(event.side or "").strip().upper()
        if side in {"BUY", "B", "1"}:
            return "BUY"
        if side in {"SELL", "S", "2"}:
            return "SELL"
        if int(getattr(event, "trade_flag", 0) or 0) == 1:
            return "BUY"
        if int(getattr(event, "trade_flag", 0) or 0) == 2:
            return "SELL"
        return ""


__all__ = ["OpeningAuctionAttitudeStrategy"]
