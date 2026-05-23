"""Collect and schedule MainSealFollow stock-pool generation.

Default source is local QMT/xtdata daily bars. The default filter is aligned
with the current iWenCai query: latest close limit-up, actual float market cap
greater than 1.9 billion yuan, 30-day max amplitude below 50%, non-ST,
main-board stocks.

Examples:
    python scripts/collect_main_seal_pool.py --once
    python scripts/collect_main_seal_pool.py --schedule-time 09:26
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from core.trading_calendar import is_market_day


DEFAULT_OUTPUT = Path("config/main_seal_follow_pool.csv")
LOCAL_RUNTIME_CONFIG_PATH = Path("config/local_runtime.json")
OUTPUT_HEADERS = ["股票代码", "名称", "计划买入金额"]
DEFAULT_SECTOR = "沪深A股"
DEFAULT_IWENCAI_QUERY = "涨停，实际流通市值大于19亿,30日最大振幅小于50%，非st，主板"
IWENCAI_COOKIE_ENV = "IWENCAI_COOKIE"
DEFAULT_JIUYANGONGSHE_USER_URL = "https://www.jiuyangongshe.com/u/4df747be1bf143a998171ef03559b517"
JIUYANGONGSHE_HOST = "https://www.jiuyangongshe.com"
IWENCAI_CODE_COLUMNS = ("股票代码", "证券代码", "代码", "code", "stock_code")
IWENCAI_NAME_COLUMNS = ("股票简称", "股票名称", "证券简称", "名称", "name", "stock_name")
IWENCAI_FLOAT_MARKET_VALUE_COLUMNS = ("实际流通市值", "自由流通市值", "流通市值")
IWENCAI_AMPLITUDE_COLUMNS = ("30日最大振幅", "最大振幅")
STOCK_TOKEN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z*]{2,12}(?:\s*A)?")
CHINESE_STOCK_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,8}(?:\s*A)?")
STOP_STOCK_TOKENS = {
    "SpaceX",
    "Nebius",
    "Rubin",
    "Optimus",
    "Gen",
    "IBM",
    "D-Wave",
    "Quantum",
    "Infleqtion",
    "Rigetti",
    "Computing",
    "No",
    "事件",
    "日常公告",
    "公告精选",
    "盘前热点事件",
    "涨停事件",
    "连板梯队",
    "昨日热点",
    "行业要闻",
    "商业航天并购",
    "自动驾驶",
    "玻璃基板",
    "机器人",
    "摘帽",
    "量子科技",
    "存储芯片",
    "算力租赁",
    "金刚石",
    "公告涨停",
    "控股股东",
    "董事长增持",
    "公司取得",
    "拟收购",
    "拟发行",
    "拟出资",
    "复牌",
    "停牌",
}


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


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _xt_name(xtdata, xt_code: str) -> str:
    try:
        detail = xtdata.get_instrument_detail(xt_code) or {}
    except Exception:
        detail = {}
    return str(detail.get("InstrumentName", "") or "").strip()


def collect_from_qmt(
    *,
    sector: str,
    chunk_size: int = 500,
    min_float_market_value: float = 1_900_000_000.0,
    max_amplitude_threshold: float = 50.0,
    history_count: int = 31,
    download_history: bool = True,
) -> list[PoolCandidate]:
    from xtquant import xtdata

    try:
        xtdata.download_sector_data()
    except Exception as exc:
        raise RuntimeError(
            "QMT 来源需要连接 QMT/xtdata 读取板块、日线和证券资料；"
            "请确认 QMT-投研版或 QMT-极简版已启动并登录。"
        ) from exc
    stock_list = [code for code in list(xtdata.get_stock_list_in_sector(sector) or []) if is_main_board_code(code)]
    candidates: list[PoolCandidate] = []

    for chunk in _chunks(stock_list, max(1, int(chunk_size or 500))):
        if download_history:
            xtdata.download_history_data2(chunk, "1d", "", "", incrementally=True)
        bars = xtdata.get_market_data_ex(
            field_list=["time", "open", "high", "low", "close", "volume", "amount"],
            stock_list=chunk,
            period="1d",
            count=max(31, int(history_count or 31)),
            dividend_type="none",
        ) or {}
        for xt_code, frame in bars.items():
            if frame is None or len(frame) < 31:
                continue
            frame = frame.tail(max(31, int(history_count or 31)))
            close_price = float(frame["close"].iloc[-1] or 0.0)
            pre_close = float(frame["close"].iloc[-2] or 0.0)
            amplitude = max_amplitude(frame["high"].tail(30).tolist(), frame["low"].tail(30).tolist())
            name = _xt_name(xtdata, xt_code)
            detail = xtdata.get_instrument_detail(xt_code) or {}
            float_volume = float(detail.get("FloatVolume", 0.0) or detail.get("FloatVolumn", 0.0) or 0.0)
            float_market_value = float_volume * close_price
            ok, pct = should_include_limitup_candidate(
                xt_code=xt_code,
                name=name,
                close_price=close_price,
                pre_close=pre_close,
                float_market_value=float_market_value,
                max_amplitude_30d=amplitude,
                min_float_market_value=float(min_float_market_value),
                max_amplitude_threshold=float(max_amplitude_threshold),
            )
            if not ok:
                continue
            candidates.append(
                PoolCandidate(
                    code=normalize_stock_code(xt_code),
                    name=name,
                    pct_change=pct,
                    last_price=close_price,
                    pre_close=pre_close,
                    amount=float(frame["amount"].iloc[-1] or 0.0),
                    float_market_value=float_market_value,
                    max_amplitude_30d=amplitude,
                )
            )

    candidates.sort(key=lambda item: (-item.float_market_value, -item.amount, item.code))
    return candidates


def collect_from_iwencai(
    *,
    query: str,
    cookie: str,
    query_type: str = "stock",
    loop: bool = True,
) -> list[PoolCandidate]:
    if not cookie:
        raise RuntimeError(
            f"iWenCai 来源需要登录后的 cookie。请通过 --iwencai-cookie、环境变量 {IWENCAI_COOKIE_ENV} "
            f"或 {LOCAL_RUNTIME_CONFIG_PATH} 传入。"
        )
    try:
        import pywencai
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError(
            "iWenCai 来源需要安装 pywencai，并确保本机 Node.js 可用。"
            "可执行: python -m pip install pywencai"
        ) from exc

    try:
        result = pywencai.get(query=query, query_type=query_type, loop=loop, cookie=cookie)
    except Exception as exc:
        raise RuntimeError(f"调用 pywencai 失败: {exc}") from exc

    if result is None:
        return []
    if isinstance(result, pd.DataFrame):
        df = result
    else:
        df = pd.DataFrame(result)
    if df.empty:
        return []

    columns = [str(column) for column in df.columns]
    code_column = find_column(columns, IWENCAI_CODE_COLUMNS)
    name_column = find_column(columns, IWENCAI_NAME_COLUMNS)
    float_market_value_column = find_column(columns, IWENCAI_FLOAT_MARKET_VALUE_COLUMNS)
    amplitude_column = find_column(columns, IWENCAI_AMPLITUDE_COLUMNS)

    if not code_column:
        raise RuntimeError(f"未能从 pywencai 结果识别股票代码列。当前列: {', '.join(columns)}")

    seen: set[str] = set()
    candidates: list[PoolCandidate] = []
    for _, item in df.iterrows():
        code = normalize_stock_code(item.get(code_column))
        if not code or code in seen or not is_main_board_code(code):
            continue
        name = str(item.get(name_column, "") or "").strip() if name_column else ""
        if name.lower() == "nan":
            name = ""
        if not is_non_st_name(name):
            continue
        seen.add(code)
        candidates.append(
            PoolCandidate(
                code=code,
                name=name,
                pct_change=0.0,
                last_price=0.0,
                pre_close=0.0,
                amount=0.0,
                float_market_value=parse_metric_number(item.get(float_market_value_column)) if float_market_value_column else 0.0,
                max_amplitude_30d=parse_metric_number(item.get(amplitude_column)) if amplitude_column else 0.0,
            )
        )
    return candidates


def _fetch_text(url: str, timeout: int = 20) -> str:
    from urllib.request import Request, urlopen

    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            )
        },
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return raw.decode("utf-8", errors="replace")


def resolve_latest_jiuyangongshe_article_url(user_url: str) -> str:
    page = _fetch_text(user_url)
    match = re.search(r'href="(/a/[0-9A-Za-z]+)"', page)
    if not match:
        match = re.search(r'canonical"\s+href="(https://www\.jiuyangongshe\.com/a/[0-9A-Za-z]+)"', page)
    if not match:
        raise RuntimeError(f"未能从用户页识别最新文章链接: {user_url}")
    href = match.group(1)
    return href if href.startswith("http") else f"{JIUYANGONGSHE_HOST}{href}"


def extract_jiuyangongshe_article_html(page: str) -> str:
    start = page.find('content:"')
    if start < 0:
        return ""
    start += len('content:"')
    end = page.find('",url:', start)
    if end < 0:
        return ""
    raw = page[start:end]
    decoded = raw.encode("utf-8").decode("unicode_escape")
    return html.unescape(decoded)


def normalize_article_plain_text(article_html: str) -> str:
    plain = re.sub(r"<[^>]+>", "\n", article_html)
    plain = html.unescape(plain)
    if "æ" in plain or "ã" in plain:
        try:
            plain = plain.encode("latin1", errors="ignore").decode("utf-8", errors="ignore")
        except Exception:
            pass
    lines = [re.sub(r"\s+", " ", line).strip() for line in plain.splitlines()]
    lines = [line for line in lines if line and line != "&nbsp;"]
    text = "\n".join(lines)
    text = re.sub(r"京东方\s*\n\s*A", "京东方A", text)
    text = re.sub(r"南玻\s*\n\s*A", "南玻A", text)
    text = re.sub(r"\n+", "\n", text)
    return text


def slice_between(text: str, start_marker: str, end_marker: str = "") -> str:
    start = text.find(start_marker)
    if start < 0:
        return ""
    start += len(start_marker)
    if not end_marker:
        return text[start:]
    end = text.find(end_marker, start)
    if end < 0:
        return text[start:]
    return text[start:end]


def extract_target_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    no1 = slice_between(text, "No.1", "No.2")
    if no1:
        sections.append(("No.1 盘前热点事件", no1))

    no2 = slice_between(text, "No.2", "No.3")
    daily = slice_between(no2, "一、日常", "二、")
    if not daily:
        daily = slice_between(no2, "一、日常公告", "二、")
    if daily:
        sections.append(("No.2 公告精选/一、日常公告", daily))

    no4 = slice_between(text, "No.4", "No.5")
    limit_events = slice_between(no4, "三、涨停事件", "")
    if limit_events:
        sections.append(("No.4 连板梯队和涨停事件/三、涨停事件", limit_events))
    return sections


def _clean_stock_token(value: str) -> str:
    token = re.sub(r"\s+", "", str(value or "").strip())
    token = token.strip("：:、，,。.（）()；;“”\"'")
    if token.endswith("A") and len(token) > 2:
        return token
    return token


def _looks_like_stock_name(token: str, known_names: Optional[set[str]] = None) -> bool:
    token = _clean_stock_token(token)
    if not token or token in STOP_STOCK_TOKENS:
        return False
    if known_names is not None:
        return token in known_names
    if len(token) < 2 or len(token) > 8:
        return False
    if re.search(r"^[一二三四五六七八九十]+$", token):
        return False
    if token.endswith(("事件", "公告", "股份事项", "动态更新")):
        return False
    return True


def extract_known_stock_names(section: str, known_names: set[str]) -> list[str]:
    matches: list[tuple[int, int, str]] = []
    for name in known_names:
        if not name:
            continue
        pattern = r"\s*".join(re.escape(char) for char in name)
        for match in re.finditer(pattern, section):
            matches.append((match.start(), -len(match.group(0)), name))

    names: list[str] = []
    seen: set[str] = set()
    accepted_spans: list[tuple[int, int]] = []
    for index, negative_length, name in sorted(matches):
        if name in seen:
            continue
        end = index - negative_length
        if any(index < accepted_end and end > accepted_start for accepted_start, accepted_end in accepted_spans):
            continue
        seen.add(name)
        accepted_spans.append((index, end))
        names.append(name)
    return names


def build_qmt_name_code_map(sector: str = DEFAULT_SECTOR) -> dict[str, str]:
    from xtquant import xtdata

    try:
        xtdata.download_sector_data()
    except Exception as exc:
        raise RuntimeError(
            "韭研公社正式股票池需要连接 QMT/xtdata 以便把股票名称解析成代码；"
            "请确认 QMT-投研版或 QMT-极简版已启动并登录。"
        ) from exc
    mapping: dict[str, str] = {}
    for xt_code in list(xtdata.get_stock_list_in_sector(sector) or []):
        code = normalize_stock_code(xt_code)
        if not code or not is_main_board_code(code):
            continue
        name = _xt_name(xtdata, xt_code)
        if name:
            mapping[name] = code
    return mapping


def extract_stock_names_from_sections(sections: list[tuple[str, str]], known_names: Optional[set[str]] = None) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    if known_names is not None:
        for _, section in sections:
            for name in extract_known_stock_names(section, known_names):
                if name in seen:
                    continue
                seen.add(name)
                names.append(name)
        return names

    for _, section in sections:
        for raw in CHINESE_STOCK_TOKEN_RE.findall(section):
            token = _clean_stock_token(raw)
            if not _looks_like_stock_name(token, known_names=known_names):
                continue
            if token in seen:
                continue
            seen.add(token)
            names.append(token)
    return names


def collect_from_jiuyangongshe(
    *,
    user_url: str,
    article_url: str,
    sector: str,
    resolve_codes: bool = True,
) -> tuple[list[PoolCandidate], str, list[tuple[str, str]]]:
    resolved_url = article_url or resolve_latest_jiuyangongshe_article_url(user_url)
    page = _fetch_text(resolved_url)
    article_html = extract_jiuyangongshe_article_html(page)
    if not article_html:
        raise RuntimeError(f"未能解析文章正文: {resolved_url}")
    text = normalize_article_plain_text(article_html)
    sections = extract_target_sections(text)

    name_code_map = build_qmt_name_code_map(sector) if resolve_codes else {}
    known_names = set(name_code_map) if name_code_map else None
    names = extract_stock_names_from_sections(sections, known_names=known_names)

    candidates: list[PoolCandidate] = []
    for name in names:
        code = name_code_map.get(name, "") if name_code_map else ""
        candidates.append(
            PoolCandidate(
                code=code or name,
                name=name,
                pct_change=0.0,
                last_price=0.0,
                pre_close=0.0,
                amount=0.0,
            )
        )
    return candidates, resolved_url, sections


def format_plan_amount(plan_amount: float) -> str:
    amount = Decimal(str(plan_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(amount.normalize(), "f")


def load_local_runtime_config() -> dict:
    config_path = Path(os.getenv("CYTRADE_LOCAL_SETTINGS_PATH", str(LOCAL_RUNTIME_CONFIG_PATH)))
    if not config_path.exists():
        return {}
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def resolve_iwencai_cookie(cli_cookie: str = "") -> str:
    if cli_cookie:
        return cli_cookie
    env_cookie = os.environ.get(IWENCAI_COOKIE_ENV, "")
    if env_cookie:
        return env_cookie
    value = load_local_runtime_config().get(IWENCAI_COOKIE_ENV, "")
    return str(value or "")


def write_pool(
    candidates: list[PoolCandidate],
    output_path: Path,
    plan_amount: float,
    *,
    backup_existing: bool = True,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_existing and output_path.exists():
        backup_path = output_path.with_name(
            f"{output_path.stem}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}{output_path.suffix}"
        )
        backup_path.write_bytes(output_path.read_bytes())

    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(OUTPUT_HEADERS)
        for item in candidates:
            writer.writerow([item.code, item.name, format_plan_amount(plan_amount)])
    temp_path.replace(output_path)


def collect_once(args) -> int:
    now = datetime.now()
    if args.market_day_only and not is_market_day(now):
        print(f"跳过股票池生成：{now:%Y-%m-%d} 不是交易日", flush=True)
        return 0

    if args.source == "jiuyangongshe":
        if args.no_resolve_codes and not args.allow_name_only_output:
            raise SystemExit(
                "jiuyangongshe source requires QMT name-code resolution for a tradable pool. "
                "Use --allow-name-only-output only for parser debugging."
            )
        candidates, article_url, sections = collect_from_jiuyangongshe(
            user_url=str(args.jiuyangongshe_user_url),
            article_url=str(args.article_url or ""),
            sector=str(args.sector),
            resolve_codes=not bool(args.no_resolve_codes),
        )
        print(
            f"已解析韭研公社文章: {article_url} sections={len(sections)} stocks={len(candidates)}",
            flush=True,
        )
    elif args.source == "iwencai":
        candidates = collect_from_iwencai(
            query=str(args.iwencai_query),
            cookie=resolve_iwencai_cookie(str(args.iwencai_cookie or "")),
            query_type=str(args.iwencai_query_type),
            loop=not bool(args.no_iwencai_loop),
        )
        print(
            f"已通过 pywencai 解析股票池: query={args.iwencai_query!r} stocks={len(candidates)}",
            flush=True,
        )
    else:
        candidates = collect_from_qmt(
            sector=str(args.sector),
            chunk_size=int(args.chunk_size),
            min_float_market_value=float(args.min_float_market_value),
            max_amplitude_threshold=float(args.max_amplitude_30d),
            history_count=int(args.history_count),
            download_history=not bool(args.no_download_history),
        )
    if args.max_count > 0:
        candidates = candidates[: int(args.max_count)]
    write_pool(
        candidates,
        Path(args.output),
        float(args.amount),
        backup_existing=not bool(args.no_backup),
    )
    print(
        (
            f"已生成股票池: {args.output} 股票数={len(candidates)} "
            f"source={args.source} amount={args.amount:g}"
        ),
        flush=True,
    )
    for item in candidates[:20]:
        print(
            f"ROW code={item.code} name={item.name} pct={item.pct_change:.2f} "
            f"last={item.last_price:.3f} pre={item.pre_close:.3f} amount={item.amount:.0f} "
            f"float_mv={item.float_market_value:.0f} amp30={item.max_amplitude_30d:.2f}",
            flush=True,
        )
    return len(candidates)


def run_scheduler(args) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    hour, minute = parse_hhmm(args.schedule_time)
    scheduler = BlockingScheduler()
    scheduler.add_job(
        collect_once,
        trigger="cron",
        day_of_week="mon-fri",
        hour=hour,
        minute=minute,
        id="collect_main_seal_pool",
        args=[args],
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    print(f"股票池定时生成器已启动：每个工作日 {args.schedule_time} 执行", flush=True)
    scheduler.start()


def parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = str(value or "").split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except Exception as exc:
        raise argparse.ArgumentTypeError("时间格式必须是 HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise argparse.ArgumentTypeError("时间范围必须在 00:00 到 23:59")
    return hour, minute


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="定时生成 MainSealFollow 股票池。")
    parser.add_argument("--source", choices=("iwencai", "qmt", "jiuyangongshe"), default="iwencai", help="股票池来源，默认 iwencai。")
    parser.add_argument("--once", action="store_true", help="只立即执行一次后退出。")
    parser.add_argument("--schedule-time", default="", help="常驻定时执行时间，格式 HH:MM。")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help=f"输出路径，默认 {DEFAULT_OUTPUT}")
    parser.add_argument("--amount", type=float, default=1000.0, help="每只股票计划买入金额，默认 1000。")
    parser.add_argument("--pct-min", type=float, default=6.0, help=argparse.SUPPRESS)
    parser.add_argument("--pct-max", type=float, default=7.0, help=argparse.SUPPRESS)
    parser.add_argument("--include-bounds", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--sector", default=DEFAULT_SECTOR, help=f"QMT 板块名，默认 {DEFAULT_SECTOR}。")
    parser.add_argument("--max-count", type=int, default=0, help="最多输出多少只，0 表示不限制。")
    parser.add_argument("--chunk-size", type=int, default=500, help="批量读取 QMT 数据的分块大小。")
    parser.add_argument("--min-float-market-value", type=float, default=1_900_000_000.0, help="QMT 来源最小实际流通市值，默认 19 亿。")
    parser.add_argument("--max-amplitude-30d", type=float, default=50.0, help="QMT 来源 30 日最大振幅上限，默认 50%。")
    parser.add_argument("--history-count", type=int, default=31, help="QMT 来源读取日线数量，默认 31。")
    parser.add_argument("--no-download-history", action="store_true", help="QMT 来源不先增量下载日线，只读取本地已有数据。")
    parser.add_argument("--iwencai-query", default=DEFAULT_IWENCAI_QUERY, help=f"iWenCai 查询语句，默认 {DEFAULT_IWENCAI_QUERY}。")
    parser.add_argument(
        "--iwencai-cookie",
        default="",
        help=f"iWenCai 登录 cookie；也可用环境变量 {IWENCAI_COOKIE_ENV} 或 {LOCAL_RUNTIME_CONFIG_PATH}。",
    )
    parser.add_argument("--iwencai-query-type", default="stock", help="pywencai query_type，默认 stock。")
    parser.add_argument("--no-iwencai-loop", action="store_true", help="pywencai 不自动翻页。")
    parser.add_argument("--jiuyangongshe-user-url", default=DEFAULT_JIUYANGONGSHE_USER_URL, help="韭研公社用户页 URL。")
    parser.add_argument("--article-url", default="", help="指定韭研公社文章 URL；为空时自动取用户页最新文章。")
    parser.add_argument("--no-resolve-codes", action="store_true", help="不通过 QMT 证券名解析代码，仅用于解析调试。")
    parser.add_argument("--allow-name-only-output", action="store_true", help="允许输出名称作为代码，仅用于调试，不能给策略实盘使用。")
    parser.add_argument("--no-backup", action="store_true", help="覆盖输出前不备份旧股票池。")
    parser.add_argument("--no-market-day-check", dest="market_day_only", action="store_false", help="非交易日也执行。")
    parser.set_defaults(market_day_only=True)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.once or not args.schedule_time:
            collect_once(args)
            return
        parse_hhmm(args.schedule_time)
        run_scheduler(args)
    except RuntimeError as exc:
        raise SystemExit(f"股票池生成失败: {exc}") from None


if __name__ == "__main__":
    main()
