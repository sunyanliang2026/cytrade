"""Dedicated overnight limit-up buy strategy for miniQMT."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

from config.enums import OrderStatus, StrategyStatus
from core.models import TickData
from monitor.logger import get_logger
from strategy.base import BaseStrategy
from strategy.models import StrategyConfig
from trading.models import Order

from .limit_price import LimitUpPriceProvider
from .order_loader import load_order_requests

logger = get_logger("trade")


@dataclass(frozen=True)
class SubmissionResult:
    stock_code: str
    amount: float
    limit_up_price: float = 0.0
    quantity: int = 0
    status: str = ""
    reason: str = ""
    order_uuid: str = ""
    submission_seq: int = 0


class OvernightLimitUpBuyStrategy(BaseStrategy):
    """Submit one BUY limit order at the daily limit-up price."""

    strategy_name = "OvernightLimitUpBuyStrategy"
    max_positions = 500
    max_total_amount = 0.0
    state_version = 1

    def __init__(self, config: StrategyConfig, trade_executor=None, position_manager=None):
        super().__init__(config, trade_executor, position_manager)
        params = dict(self.config.params or {})
        self._amount = float(params.get("amount", 0.0) or 0.0)
        self._csv_path = str(params.get("csv_path") or self._default_csv_path())
        self._source_row = int(params.get("source_row", 0) or 0)
        self._request_key = str(params.get("request_key") or f"stock:{self.stock_code}")
        self._submitted_trade_days = set(str(item) for item in (params.get("submitted_trade_days") or []))

    @classmethod
    def required_data_kinds(cls) -> set[str]:
        return set()

    def select_stocks(self) -> list[StrategyConfig]:
        csv_path = Path(self.config.params.get("csv_path") or self._default_csv_path())
        configs: list[StrategyConfig] = []
        for request in load_order_requests(csv_path):
            configs.append(
                StrategyConfig(
                    stock_code=request.stock_code,
                    params={
                        "amount": request.amount,
                        "csv_path": str(csv_path),
                        "source_row": request.row_no,
                        "request_key": f"row:{request.row_no}",
                        "instance_key": f"overnight_limit_up_buy:row:{request.row_no}",
                    },
                )
            )
        return configs

    def on_tick(self, tick: TickData) -> Optional[dict]:
        return None

    def submit_once(
        self,
        *,
        trade_day: str | date | datetime,
        price_lookup: Callable[[str], float] | None = None,
        submission_store=None,
        record_submission: bool = True,
        log_submission: bool = True,
    ) -> SubmissionResult:
        trade_day_text = format_trade_day(trade_day)
        if self.status != StrategyStatus.RUNNING:
            return self._result(status="skipped", reason="strategy_not_running")
        if self._amount <= 0:
            return self._result(status="skipped", reason="invalid_amount")
        if trade_day_text in self._submitted_trade_days:
            return self._result(status="skipped", reason="already_submitted_in_memory")
        if submission_store and submission_store.was_submitted(trade_day_text, self._request_key):
            self._submitted_trade_days.add(trade_day_text)
            return self._result(status="skipped", reason="already_submitted_state_file")

        lookup = price_lookup or LimitUpPriceProvider().get_limit_up_price
        limit_up_price = float(lookup(self.stock_code) or 0.0)
        if limit_up_price <= 0:
            return self._result(
                status="skipped",
                reason="limit_up_price_unavailable",
                limit_up_price=limit_up_price,
            )

        quantity = calculate_order_quantity(self._amount, limit_up_price)
        if quantity <= 0:
            return self._result(
                status="skipped",
                reason="amount_less_than_one_lot",
                limit_up_price=limit_up_price,
                quantity=quantity,
            )
        if not self._trade_executor:
            return self._result(
                status="failed",
                reason="trade_executor_missing",
                limit_up_price=limit_up_price,
                quantity=quantity,
            )

        remark = (
            "overnight_limit_up_buy "
            f"trade_day={trade_day_text} amount={self._amount:.2f} "
            f"limit_up={limit_up_price:.3f} qty={quantity}"
        )
        order = self.add_position(limit_up_price, quantity, remark=remark)
        if not order:
            return self._result(
                status="failed",
                reason="order_not_created",
                limit_up_price=limit_up_price,
                quantity=quantity,
            )
        if getattr(order, "status", None) == OrderStatus.JUNK:
            return self._result(
                status="rejected",
                reason=str(getattr(order, "status_msg", "") or "order_rejected"),
                limit_up_price=limit_up_price,
                quantity=quantity,
                order=order,
            )

        self._submitted_trade_days.add(trade_day_text)
        if submission_store and record_submission:
            submission_store.record(
                trade_day_text,
                self._request_key,
                {
                    "amount": self._amount,
                    "source_row": self._source_row,
                    "stock_code": self.stock_code,
                    "limit_up_price": limit_up_price,
                    "quantity": quantity,
                    "order_uuid": getattr(order, "order_uuid", ""),
                    "order_trace_id": getattr(order, "order_trace_id", ""),
                },
            )
        if log_submission:
            logger.info(
                "OVERNIGHT_LIMIT_UP_BUY submitted stock=%s trade_day=%s amount=%.2f price=%.3f qty=%d order_uuid=%s",
                self.stock_code, trade_day_text, self._amount, limit_up_price, quantity,
                getattr(order, "order_uuid", "")[:8],
            )
        return self._result(
            status="submitted",
            reason="",
            limit_up_price=limit_up_price,
            quantity=quantity,
            order=order,
        )

    @property
    def request_key(self) -> str:
        return self._request_key

    @property
    def source_row(self) -> int:
        return self._source_row

    def _result(
        self,
        *,
        status: str,
        reason: str,
        limit_up_price: float = 0.0,
        quantity: int = 0,
        order: Order | None = None,
    ) -> SubmissionResult:
        return SubmissionResult(
            stock_code=self.stock_code,
            amount=self._amount,
            limit_up_price=float(limit_up_price or 0.0),
            quantity=int(quantity or 0),
            status=status,
            reason=reason,
            order_uuid=str(getattr(order, "order_uuid", "") or ""),
            submission_seq=int(getattr(order, "xt_fields", {}).get("submit_seq", 0) or 0),
        )

    @staticmethod
    def _default_csv_path() -> Path:
        return Path(__file__).resolve().parent / "data" / "orders.csv"


def calculate_order_quantity(amount: float, limit_up_price: float) -> int:
    if amount <= 0 or limit_up_price <= 0:
        return 0
    return int((float(amount) / float(limit_up_price)) // 100) * 100


def format_trade_day(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text
