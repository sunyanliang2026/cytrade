from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, Optional


DEFAULT_OUTPUT = Path("data/stock_pools/current/main_seal_follow_pool.csv")
DEFAULT_TRACE_DIR = Path("data/stock_pools/runs")
LOCAL_RUNTIME_CONFIG_PATH = Path("config/local_runtime.json")
DEFAULT_SOURCE_CONFIG = Path("config/main_seal_pool_sources.json")
DEFAULT_IWENCAI_QUERY_FILE = Path("config/iwencai_pool_queries.json")
OUTPUT_HEADERS = ["股票代码", "名称", "计划买入金额"]
DEFAULT_SECTOR = "沪深A股"
DEFAULT_IWENCAI_QUERY = "涨停，实际流通市值大于19亿,30日最大振幅小于50%，非st，主板"
IWENCAI_COOKIE_ENV = "IWENCAI_COOKIE"
DEFAULT_JIUYANGONGSHE_USER_URL = "https://www.jiuyangongshe.com/u/4df747be1bf143a998171ef03559b517"
JIUYANGONGSHE_HOST = "https://www.jiuyangongshe.com"


@dataclass(frozen=True)
class PoolCandidate:
    code: str
    name: str
    pct_change: float
    last_price: float
    pre_close: float
    amount: float
    float_market_value: float = 0.0
    max_amplitude_30d: float = 0.0


@dataclass(frozen=True)
class IwencaiQuery:
    name: str
    type: str
    query: str


@dataclass(frozen=True)
class JiuyangongsheConfig:
    enabled: bool
    user_url: str
    require_today: bool = True


@dataclass(frozen=True)
class SourceSet:
    name: str
    source: str
    query: str = ""
    node: str = ""


def normalize_stock_code(value: str) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6) if text.isdigit() else ""


def normalize_column_name(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def find_column(columns: list[str], candidates: tuple[str, ...]) -> str:
    normalized_map = {normalize_column_name(column): column for column in columns}
    for candidate in candidates:
        column = normalized_map.get(normalize_column_name(candidate))
        if column:
            return column
    for column in columns:
        normalized = normalize_column_name(column).lower()
        if any(normalize_column_name(candidate).lower() in normalized for candidate in candidates):
            return column
    return ""


def parse_metric_number(value: object) -> float:
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return 0.0
    multiplier = 1.0
    if "亿" in text:
        multiplier = 100_000_000.0
    elif "万" in text:
        multiplier = 10_000.0
    text = text.replace(",", "").replace("%", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    return float(match.group(0)) * multiplier


def is_main_board_code(xt_code: str) -> bool:
    code = normalize_stock_code(xt_code)
    if not code:
        return False
    return code.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))


def is_non_st_name(name: str) -> bool:
    normalized = str(name or "").strip().upper().replace(" ", "")
    if not normalized:
        return True
    return "ST" not in normalized and "退" not in normalized


def pct_change(last_price: float, pre_close: float) -> Optional[float]:
    if pre_close <= 0 or last_price <= 0:
        return None
    return (last_price / pre_close - 1.0) * 100.0


def round_price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def limit_up_price(pre_close: float, limit_ratio: float = 0.10) -> float:
    if pre_close <= 0:
        return 0.0
    return round_price(pre_close * (1.0 + limit_ratio))


def is_limit_up_close(close_price: float, pre_close: float, limit_ratio: float = 0.10) -> bool:
    target = limit_up_price(pre_close, limit_ratio=limit_ratio)
    return target > 0 and close_price >= target - 1e-6


def max_amplitude(high_prices: Iterable[float], low_prices: Iterable[float]) -> float:
    highs = [float(value or 0) for value in high_prices if float(value or 0) > 0]
    lows = [float(value or 0) for value in low_prices if float(value or 0) > 0]
    if not highs or not lows:
        return 0.0
    low = min(lows)
    if low <= 0:
        return 0.0
    return (max(highs) / low - 1.0) * 100.0


def should_include_candidate(
    *,
    xt_code: str,
    name: str,
    last_price: float,
    pre_close: float,
    pct_min: float,
    pct_max: float,
    include_bounds: bool = False,
) -> tuple[bool, float]:
    pct = pct_change(last_price, pre_close)
    if pct is None:
        return False, 0.0
    if not is_main_board_code(xt_code):
        return False, pct
    if not is_non_st_name(name):
        return False, pct
    if include_bounds:
        return pct_min <= pct <= pct_max, pct
    return pct_min < pct < pct_max, pct


def should_include_limitup_candidate(
    *,
    xt_code: str,
    name: str,
    close_price: float,
    pre_close: float,
    float_market_value: float,
    max_amplitude_30d: float,
    min_float_market_value: float,
    max_amplitude_threshold: float,
) -> tuple[bool, float]:
    pct = pct_change(close_price, pre_close) or 0.0
    if not is_main_board_code(xt_code):
        return False, pct
    if not is_non_st_name(name):
        return False, pct
    if not is_limit_up_close(close_price, pre_close):
        return False, pct
    if float_market_value <= min_float_market_value:
        return False, pct
    if max_amplitude_30d <= 0 or max_amplitude_30d >= max_amplitude_threshold:
        return False, pct
    return True, pct


def format_plan_amount(plan_amount: float) -> str:
    amount = Decimal(str(plan_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(amount.normalize(), "f")
