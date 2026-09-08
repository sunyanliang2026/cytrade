"""Limit-up price lookup helpers for miniQMT/xtdata."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

try:
    from xtquant import xtdata

    _XT_AVAILABLE = True
except ImportError:
    xtdata = None  # type: ignore
    _XT_AVAILABLE = False


LIMIT_UP_KEYS = (
    "upperLimit",
    "UpperLimit",
    "UpStopPrice",
    "upStopPrice",
    "up_stop_price",
    "upLimitPrice",
    "upperLimitPrice",
    "limitUp",
    "upper_limit",
)


class LimitUpPriceProvider:
    """Read today's limit-up price, never knowingly reusing another day."""

    def __init__(self, *, connect_xtdata: bool = True):
        self._connect_xtdata = bool(connect_xtdata)
        self._connected = False

    def get_limit_up_price(self, stock_code: str, expected_trade_day: date | None = None) -> float:
        """Return today's limit-up price, or 0.0 when QMT cannot provide it."""

        return self.get_limit_up_quote(stock_code, expected_trade_day)[0]

    def get_limit_up_quote(self, stock_code: str, expected_trade_day: date | None = None) -> tuple[float, float]:
        """Return verified (limit_up_price, previous_close) for a trade day."""

        if not _XT_AVAILABLE or xtdata is None:
            return 0.0, 0.0
        self._ensure_connected()

        xt_code = to_xt_code(stock_code)
        if not xt_code:
            return 0.0, 0.0

        target_day = expected_trade_day or date.today()
        detail = self._get_instrument_detail(xt_code)
        previous_close = self._get_previous_close(xt_code, target_day)
        derived_price = calculate_limit_up_price(previous_close, stock_code, detail) if previous_close > 0 else 0.0
        tick_price = self._from_full_tick(xt_code, stock_code, target_day)
        if derived_price > 0:
            if tick_price > 0 and not prices_equal(tick_price, derived_price):
                return 0.0, previous_close
            return derived_price, previous_close
        return 0.0, 0.0

    def get_exact_limit_up_price(self, stock_code: str, expected_trade_day: date | None = None) -> float:
        """Return only an explicit limit price supplied by today's QMT data."""
        if not _XT_AVAILABLE or xtdata is None:
            return 0.0
        self._ensure_connected()
        xt_code = to_xt_code(stock_code)
        if not xt_code:
            return 0.0
        target_day = expected_trade_day or date.today()
        detail = self._get_instrument_detail(xt_code)
        if detail_day_is_current(detail, target_day):
            price = extract_limit_up_price(detail)
            if price > 0:
                return price
        return self._from_full_tick(xt_code, stock_code, target_day)

    def _ensure_connected(self) -> None:
        if self._connected or not self._connect_xtdata or xtdata is None:
            return
        connect = getattr(xtdata, "connect", None)
        if callable(connect):
            try:
                connect()
            except Exception:
                pass
        self._connected = True

    def _from_instrument_detail(self, xt_code: str) -> float:
        detail = self._get_instrument_detail(xt_code)
        if not detail_day_is_current(detail):
            return 0.0
        return extract_limit_up_price(detail)

    def _get_instrument_detail(self, xt_code: str) -> dict:
        getter = getattr(xtdata, "get_instrument_detail", None) if xtdata is not None else None
        if not callable(getter):
            return {}
        try:
            detail = getter(xt_code, iscomplete=True)
        except TypeError:
            try:
                detail = getter(xt_code)
            except Exception:
                return {}
        except Exception:
            return {}
        return detail if isinstance(detail, dict) else {}

    def _from_full_tick(self, xt_code: str, stock_code: str, expected_trade_day: date | None = None) -> float:
        getter = getattr(xtdata, "get_full_tick", None) if xtdata is not None else None
        if not callable(getter):
            return 0.0
        try:
            tick_map = getter([xt_code]) or {}
        except Exception:
            return 0.0
        if not isinstance(tick_map, dict):
            return 0.0
        payload = tick_map.get(xt_code) or tick_map.get(stock_code) or {}
        if not tick_day_is_current(payload, expected_trade_day):
            return 0.0
        return extract_limit_up_price(payload)

    def _get_previous_close(self, xt_code: str, target_day: date) -> float:
        getter = getattr(xtdata, "get_market_data_ex", None) if xtdata is not None else None
        if not callable(getter):
            return 0.0
        try:
            raw = getter(
                field_list=["close"], stock_list=[xt_code], period="1d",
                start_time="", end_time=target_day.strftime("%Y%m%d"),
                count=2, dividend_type="none", fill_data=False,
            ) or {}
        except Exception:
            return 0.0
        frame = raw.get(xt_code) if isinstance(raw, dict) else None
        if frame is None or getattr(frame, "empty", True):
            return 0.0
        closes = []
        series = frame["close"] if hasattr(frame, "__getitem__") and "close" in frame else None
        if series is None:
            return 0.0
        for index, value in series.items():
            observed_day = normalize_day(index)
            if observed_day and observed_day < target_day:
                try:
                    closes.append(float(value))
                except (TypeError, ValueError):
                    pass
        if not closes:
            return 0.0
        return closes[-1]


def detail_day_is_current(payload: Any, today: date | None = None) -> bool:
    """Return false when a complete instrument detail identifies another day.

    A missing day is not sufficient evidence for a pre-open order: some QMT
    builds keep the previous day's ``UpStopPrice`` in the detail cache.
    """

    if not isinstance(payload, dict):
        return False
    raw_day = payload.get("TradingDay") or payload.get("tradingDay")
    if raw_day in (None, ""):
        return False
    return normalize_day(raw_day) == (today or date.today())


def tick_day_is_current(payload: Any, today: date | None = None) -> bool:
    """Reject a snapshot whose timestamp is explicitly from a prior day."""

    if not isinstance(payload, dict):
        return False
    raw_time = payload.get("time") or payload.get("timestamp") or payload.get("dataTime")
    if raw_time in (None, "", 0):
        return False
    try:
        value = float(raw_time)
        if value > 10_000_000_000:
            value /= 1000.0
        observed = datetime.fromtimestamp(value).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return True
    return observed == (today or date.today())


def prices_equal(left: float, right: float) -> bool:
    return abs(float(left) - float(right)) <= 0.005


def calculate_limit_up_price(previous_close: float, stock_code: str, detail: dict | None = None) -> float:
    """Calculate the regular A-share daily upper limit from the prior close."""
    code = str(stock_code or "").split(".", 1)[0].zfill(6)
    name = str((detail or {}).get("InstrumentName") or (detail or {}).get("instrumentName") or "").upper()
    if "ST" in name or "*ST" in name:
        ratio = Decimal("1.05")
    elif code.startswith(("300", "301", "688")):
        ratio = Decimal("1.20")
    elif code.startswith(("8", "4", "92")):
        ratio = Decimal("1.30")
    else:
        ratio = Decimal("1.10")
    value = (Decimal(str(previous_close)) * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(value)


def normalize_day(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        text = text[:10]
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def extract_limit_up_price(payload: Any) -> float:
    """Extract a positive limit-up price from xtdata-style payloads."""

    if not isinstance(payload, dict):
        return 0.0
    for key in LIMIT_UP_KEYS:
        value = payload.get(key)
        if isinstance(value, (list, tuple)) and value:
            value = value[0]
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return 0.0


def to_xt_code(stock_code: str) -> str:
    code = str(stock_code or "").strip().upper()
    if "." in code:
        code = code.split(".", 1)[0]
    if len(code) != 6 or not code.isdigit():
        return ""
    if code.startswith(("5", "6", "9", "11")):
        return f"{code}.SH"
    return f"{code}.SZ"
