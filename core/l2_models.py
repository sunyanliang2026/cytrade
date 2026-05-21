"""Standardized Level2 market data models.

These models keep L2-specific fields out of ``TickData`` so ordinary
tick-driven strategies remain unchanged, while L2-driven strategies can
consume richer data through dedicated callbacks.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class L2QuoteEvent:
    stock_code: str = ""
    last_price: float = 0.0
    pre_close: float = 0.0
    bid1: float = 0.0
    ask1: float = 0.0
    limit_up_price: float = 0.0
    event_time: Optional[datetime] = None
    recv_time: Optional[datetime] = None
    raw_xt_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class L2TransactionEvent:
    stock_code: str = ""
    price: float = 0.0
    volume: int = 0
    amount: float = 0.0
    side: str = ""
    event_time: Optional[datetime] = None
    recv_time: Optional[datetime] = None
    raw_xt_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class L2OrderEvent:
    stock_code: str = ""
    price: float = 0.0
    volume: int = 0
    amount: float = 0.0
    side: str = ""
    entrust_no: str = ""
    is_cancel: bool = False
    event_time: Optional[datetime] = None
    recv_time: Optional[datetime] = None
    raw_xt_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class L2OrderQueueEvent:
    stock_code: str = ""
    price: float = 0.0
    bid_level_volume: list[int] = field(default_factory=list)
    event_time: Optional[datetime] = None
    recv_time: Optional[datetime] = None
    raw_xt_fields: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "L2QuoteEvent",
    "L2TransactionEvent",
    "L2OrderEvent",
    "L2OrderQueueEvent",
]
