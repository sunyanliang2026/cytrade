"""Data models for opening-auction attitude scoring.

The models in this module are pure data containers. They do not subscribe to
market data, write logs, or place orders.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any


AUCTION_NO_SIGNAL = "NO_SIGNAL"
AUCTION_SPEED_ONLY = "AUCTION_SPEED_ONLY"
AUCTION_MONEY_LIFT = "AUCTION_MONEY_LIFT"
AUCTION_BIG_ORDER_CONFIRMED = "AUCTION_BIG_ORDER_CONFIRMED"
AUCTION_BIG_TRADE_CONFIRMED = "AUCTION_BIG_TRADE_CONFIRMED"
AUCTION_STRONG_CONFIRMED = "AUCTION_STRONG_CONFIRMED"
AUCTION_FAKE_RISK = "AUCTION_FAKE_RISK"

OPEN_DIRECT_PULL = "DIRECT_PULL"
OPEN_WASH_THEN_PULL = "WASH_THEN_PULL"
OPEN_FAKE_BREAKDOWN = "FAKE_BREAKDOWN"
OPEN_NO_FOLLOW_THROUGH = "NO_FOLLOW_THROUGH"


@dataclass(slots=True)
class AuctionPricePoint:
    """One cumulative auction price/amount observation."""

    event_time: datetime | None
    price: float
    matched_amount: float = 0.0
    matched_volume: float = 0.0
    unmatched_buy_volume: float = 0.0
    unmatched_sell_volume: float = 0.0
    unmatched_buy_amount: float = 0.0
    unmatched_sell_amount: float = 0.0
    amount_source: str = ""


@dataclass(slots=True)
class AuctionL1Window:
    """L1 auction observations for one symbol."""

    symbol: str
    pre_close: float
    points: list[AuctionPricePoint] = field(default_factory=list)


@dataclass(slots=True)
class AuctionL2Window:
    """Aggregated Level2 evidence inside the auction scoring window."""

    symbol: str
    l2quote_count: int = 0
    l2order_count: int = 0
    l2transaction_count: int = 0
    l2orderqueue_count: int = 0
    big_buy_order_amount: float = 0.0
    big_sell_order_amount: float = 0.0
    cancel_buy_order_amount: float = 0.0
    cancel_sell_order_amount: float = 0.0
    big_buy_trade_amount: float = 0.0
    big_sell_trade_amount: float = 0.0


@dataclass(slots=True)
class AuctionScoreConfig:
    """Tunable scoring thresholds for the opening-auction attitude model."""

    window_start: time = time(9, 24, 30)
    window_end: time = time(9, 25, 5)
    min_low_to_final_lift_pct: float = 0.005
    min_final_gap_pct: float = 0.015
    min_money_lift_ratio: float = 0.30
    strong_money_lift_ratio: float = 0.50
    close_to_high_tolerance_pct: float = 0.001
    big_buy_ratio_threshold: float = 0.60
    sell_pressure_ratio_threshold: float = 0.60
    unmatched_sell_pressure_ratio_threshold: float = 0.60


@dataclass(slots=True)
class AuctionWindowMetrics:
    """Derived measurements used by scoring and replay output."""

    symbol: str
    auction_low_price: float = 0.0
    auction_low_time: datetime | None = None
    auction_final_price: float = 0.0
    auction_final_time: datetime | None = None
    auction_high_price: float = 0.0
    auction_high_time: datetime | None = None
    final_gap_pct: float = 0.0
    low_to_final_lift_pct: float = 0.0
    low_to_final_amount_delta: float = 0.0
    low_to_final_amount_ratio: float = 0.0
    amount_at_low: float = 0.0
    amount_at_final: float = 0.0
    amount_source_at_low: str = ""
    amount_source_at_final: str = ""
    amount_is_cumulative: bool = True
    matched_volume_at_low: float = 0.0
    matched_volume_at_final: float = 0.0
    unmatched_buy_volume_at_final: float = 0.0
    unmatched_sell_volume_at_final: float = 0.0
    unmatched_buy_amount_at_final: float = 0.0
    unmatched_sell_amount_at_final: float = 0.0
    unmatched_amount_imbalance_at_final: float = 0.0
    has_unmatched_sell_pressure: bool = False
    final_near_high: bool = False
    big_order_imbalance: float = 0.0
    big_trade_imbalance: float = 0.0
    big_order_buy_ratio: float = 0.0
    big_trade_buy_ratio: float = 0.0
    has_order_confirmation: bool = False
    has_trade_data: bool = False
    has_trade_confirmation: bool = False
    has_order_sell_pressure: bool = False
    has_trade_sell_pressure: bool = False


@dataclass(slots=True)
class AuctionDecision:
    """Final opening-auction attitude decision."""

    symbol: str
    auction_label: str
    auction_speed_score: float
    auction_attitude_score: float
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OpenVerifyPoint:
    """One post-open price/amount observation."""

    event_time: datetime | None
    price: float
    amount: float = 0.0
    volume: float = 0.0


@dataclass(slots=True)
class OpenVerifyWindow:
    """09:30-09:35 behavior observations for one symbol."""

    symbol: str
    auction_label: str = ""
    auction_final_price: float = 0.0
    points: list[OpenVerifyPoint] = field(default_factory=list)
    buy_trade_amount: float = 0.0
    sell_trade_amount: float = 0.0


@dataclass(slots=True)
class OpenVerifyConfig:
    """Tunable thresholds for post-open true/false auction verification."""

    window_start: time = time(9, 30, 0)
    window_end: time = time(9, 35, 0)
    direct_check_start_sec: int = 5
    direct_check_end_sec: int = 45
    min_direct_pull_pct: float = 0.005
    max_direct_drawdown_pct: float = 0.003
    min_wash_dip_pct: float = 0.003
    max_wash_drawdown_pct: float = 0.03
    recover_open_tolerance_pct: float = 0.001
    min_rebreak_pct: float = 0.001
    breakdown_pct: float = 0.008
    failed_recover_tolerance_pct: float = 0.001
    min_buy_trade_ratio: float = 0.55
    sell_pressure_ratio_threshold: float = 0.55


@dataclass(slots=True)
class OpenVerifyMetrics:
    """Derived open verification measurements."""

    symbol: str
    point_count: int = 0
    open_price: float = 0.0
    open_time: datetime | None = None
    high_price: float = 0.0
    high_time: datetime | None = None
    low_price: float = 0.0
    low_time: datetime | None = None
    final_price: float = 0.0
    final_time: datetime | None = None
    direct_high_price: float = 0.0
    direct_high_time: datetime | None = None
    direct_high_gain_pct: float = 0.0
    direct_low_drawdown_pct: float = 0.0
    max_gain_from_open_pct: float = 0.0
    max_drawdown_from_open_pct: float = 0.0
    final_return_pct: float = 0.0
    seconds_to_high: float = 0.0
    first_rebound_high_price: float = 0.0
    recovered_open: bool = False
    rebreak_after_recover: bool = False
    buy_trade_ratio: float = 0.0
    sell_trade_ratio: float = 0.0
    has_trade_data: bool = False
    has_buy_confirmation: bool = False
    has_sell_pressure: bool = False


@dataclass(slots=True)
class OpenVerifyDecision:
    """Final 09:30-09:35 verification decision."""

    symbol: str
    open_verify_path: str
    open_verify_score: float
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "AUCTION_NO_SIGNAL",
    "AUCTION_SPEED_ONLY",
    "AUCTION_MONEY_LIFT",
    "AUCTION_BIG_ORDER_CONFIRMED",
    "AUCTION_BIG_TRADE_CONFIRMED",
    "AUCTION_STRONG_CONFIRMED",
    "AUCTION_FAKE_RISK",
    "OPEN_DIRECT_PULL",
    "OPEN_WASH_THEN_PULL",
    "OPEN_FAKE_BREAKDOWN",
    "OPEN_NO_FOLLOW_THROUGH",
    "AuctionPricePoint",
    "AuctionL1Window",
    "AuctionL2Window",
    "AuctionScoreConfig",
    "AuctionWindowMetrics",
    "AuctionDecision",
    "OpenVerifyPoint",
    "OpenVerifyWindow",
    "OpenVerifyConfig",
    "OpenVerifyMetrics",
    "OpenVerifyDecision",
]
