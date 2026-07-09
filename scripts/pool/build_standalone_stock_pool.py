"""Build an independent WenCai stock pool in jingjiabuy.csv format."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pool.common import is_main_board_code, is_non_st_name, normalize_column_name, normalize_stock_code
from scripts.pool.iwencai_source import resolve_iwencai_cookie
from scripts.pool.source_config import read_source_sets_from_config


DEFAULT_SOURCE_CONFIG = Path("config/standalone_stock_pool_sources.json")
DEFAULT_OUTPUT = Path("data/standalone_stock_pool/jingjiabuy.csv")
DEFAULT_TRACE_DIR = Path("data/standalone_stock_pool/runs")
DEFAULT_SOURCE_CACHE_DIR = Path("data/standalone_stock_pool/source_cache")
OUTPUT_HEADERS = ["symbol", "buy_amount", "ignore", "level", "nick", "max_amt"]

CODE_COLUMNS = ("symbol", "stock_code", "code", "股票代码", "证券代码", "代码")
NAME_COLUMNS = ("nick", "stock_name", "name", "股票简称", "股票名称", "证券简称", "名称")
MAX_AMOUNT_COLUMNS = (
    "max_amt",
    "区间最高成交额",
    "最近50日单日最高成交额",
    "最近50日最高成交额",
    "单日最高成交额",
    "最高成交额",
)
BOARD_COLUMNS = ("level", "几天几板", "几日几板", "连板", "涨停天数", "涨停类型")
FIRST_LIMIT_COLUMNS = ("首次涨停时间",)
FINAL_LIMIT_COLUMNS = ("最终涨停时间",)
PCT_COLUMNS = ("最新涨跌幅", "涨跌幅")


@dataclass(frozen=True)
class JingjiaBuyRow:
    symbol: str
    buy_amount: int
    ignore: int
    level: int
    nick: str
    max_amt: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build independent jingjiabuy.csv from configured WenCai queries.")
    parser.add_argument("--source-config", default=str(DEFAULT_SOURCE_CONFIG))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--trace-dir", default=str(DEFAULT_TRACE_DIR))
    parser.add_argument("--source-cache-dir", default=str(DEFAULT_SOURCE_CACHE_DIR))
    parser.add_argument("--buy-amount", type=int, default=31000)
    parser.add_argument("--ignore", type=int, default=0)
    parser.add_argument("--iwencai-cookie", default="")
    parser.add_argument("--iwencai-query-type", default="stock")
    parser.add_argument("--no-iwencai-loop", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    return parser


def market_symbol(code: str) -> str:
    normalized = normalize_any_stock_code(code)
    if not normalized:
        return ""
    if normalized.startswith("6"):
        return f"SHSE.{normalized}"
    if normalized.startswith(("0", "2", "3")):
        return f"SZSE.{normalized}"
    if normalized.startswith(("4", "8")):
        return f"BJSE.{normalized}"
    return normalized


def normalize_any_stock_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.startswith(("SHSE.", "SZSE.", "BJSE.")):
        text = text.split(".", 1)[1]
    return normalize_stock_code(text)


def find_fuzzy_column(columns: Iterable[str], candidates: Iterable[str]) -> str:
    column_list = [str(column) for column in columns]
    normalized_map = {normalize_column_name(column).lower(): column for column in column_list}
    for candidate in candidates:
        found = normalized_map.get(normalize_column_name(candidate).lower())
        if found:
            return found
    normalized_candidates = [normalize_column_name(candidate).lower() for candidate in candidates]
    for column in column_list:
        normalized = normalize_column_name(column).lower()
        if any(candidate and candidate in normalized for candidate in normalized_candidates):
            return column
    return ""


def parse_number(value: Any) -> int:
    text = str(value or "").strip().replace(",", "")
    if not text or text.lower() == "nan":
        return 0
    multiplier = 1.0
    if "万" in text:
        multiplier = 10_000.0
    elif "亿" in text:
        multiplier = 100_000_000.0
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned:
        return 0
    try:
        return int(round(float(cleaned) * multiplier))
    except ValueError:
        return 0


def _chinese_number_to_int(text: str) -> int:
    mapping = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = mapping.get(left, 1) if left else 1
        ones = mapping.get(right, 0) if right else 0
        return tens * 10 + ones
    return mapping.get(text, 0)


def parse_board_level(value: Any) -> int:
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return 1
    if "一字" in text:
        return 2
    if "首板" in text:
        return 1
    numbers = [int(item) for item in re.findall(r"\d+", text)]
    chinese_numbers = [_chinese_number_to_int(item) for item in re.findall(r"[一二两三四五六七八九十]+", text)]
    all_numbers = [item for item in numbers + chinese_numbers if item > 0]
    if any(item > 1 for item in all_numbers):
        return 2
    return 1


def parse_time_seconds(value: Any) -> int:
    text = str(value or "").strip()
    match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if not match:
        return 24 * 60 * 60
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)
    return hour * 3600 + minute * 60 + second


def parse_float(value: Any) -> float:
    text = str(value or "").strip().replace(",", "")
    if not text or text.lower() == "nan":
        return 0.0
    cleaned = re.sub(r"[^0-9.\-]", "", text)
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _is_first_board_text(value: Any) -> bool:
    text = str(value or "")
    return "首板" in text or "1天1板" in text or "1日1板" in text or "一天一板" in text or "一日一板" in text


def _is_open_limit_board(record: dict[str, Any], first_limit_column: str, final_limit_column: str) -> bool:
    first_seconds = parse_time_seconds(record.get(first_limit_column))
    final_seconds = parse_time_seconds(record.get(final_limit_column))
    return first_seconds <= parse_time_seconds("09:25:05") and first_seconds == final_seconds


def parse_record_level(record: dict[str, Any], board_column: str, first_limit_column: str, final_limit_column: str) -> int:
    board_text = record.get(board_column)
    base_level = parse_board_level(board_text)
    if base_level > 1:
        return 2
    if _is_first_board_text(board_text) and _is_open_limit_board(record, first_limit_column, final_limit_column):
        return 2
    return 1


def record_sort_key(
    record: dict[str, Any],
    row: JingjiaBuyRow,
    *,
    board_column: str,
    first_limit_column: str,
    pct_column: str,
) -> tuple[int, int, float, str]:
    board_text = record.get(board_column)
    if parse_board_level(board_text) > 1:
        group = 0
    elif row.level > 1:
        group = 1
    else:
        group = 2
    return (
        group,
        parse_time_seconds(record.get(first_limit_column)),
        -parse_float(record.get(pct_column)),
        row.symbol,
    )


def rows_from_records(records: Iterable[dict[str, Any]], *, buy_amount: int = 31000, ignore: int = 0) -> list[JingjiaBuyRow]:
    rows = list(records)
    if not rows:
        return []
    columns = list(rows[0].keys())
    code_column = find_fuzzy_column(columns, CODE_COLUMNS)
    name_column = find_fuzzy_column(columns, NAME_COLUMNS)
    max_amount_column = find_fuzzy_column(columns, MAX_AMOUNT_COLUMNS)
    board_column = find_fuzzy_column(columns, BOARD_COLUMNS)
    first_limit_column = find_fuzzy_column(columns, FIRST_LIMIT_COLUMNS)
    final_limit_column = find_fuzzy_column(columns, FINAL_LIMIT_COLUMNS)
    pct_column = find_fuzzy_column(columns, PCT_COLUMNS)
    if not code_column:
        raise RuntimeError(f"standalone stock pool cannot identify code column: {', '.join(columns)}")
    if not name_column:
        raise RuntimeError(f"standalone stock pool cannot identify name column: {', '.join(columns)}")
    if not max_amount_column:
        raise RuntimeError(f"standalone stock pool cannot identify max amount column: {', '.join(columns)}")
    if not board_column:
        raise RuntimeError(f"standalone stock pool cannot identify board-level column: {', '.join(columns)}")
    if not first_limit_column:
        raise RuntimeError(f"standalone stock pool cannot identify first-limit-time column: {', '.join(columns)}")
    if not final_limit_column:
        raise RuntimeError(f"standalone stock pool cannot identify final-limit-time column: {', '.join(columns)}")

    result: list[tuple[tuple[int, int, float, str], JingjiaBuyRow]] = []
    seen: set[str] = set()
    for record in rows:
        code = normalize_any_stock_code(record.get(code_column))
        if not code or code in seen:
            continue
        name = str(record.get(name_column, "") or "").strip()
        if name.lower() == "nan":
            name = ""
        if not is_main_board_code(code) or not is_non_st_name(name):
            continue
        symbol = market_symbol(code)
        if not symbol:
            continue
        seen.add(code)
        row = JingjiaBuyRow(
            symbol=symbol,
            buy_amount=int(buy_amount),
            ignore=int(ignore),
            level=parse_record_level(record, board_column, first_limit_column, final_limit_column),
            nick=name,
            max_amt=parse_number(record.get(max_amount_column)),
        )
        result.append(
            (
                record_sort_key(
                    record,
                    row,
                    board_column=board_column,
                    first_limit_column=first_limit_column,
                    pct_column=pct_column,
                ),
                row,
            )
        )
    return [row for _, row in sorted(result, key=lambda item: item[0])]


def write_jingjiabuy(rows: list[JingjiaBuyRow], output_path: Path, *, backup_existing: bool = True) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_existing and output_path.exists():
        backup_path = output_path.with_name(
            f"{output_path.stem}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}{output_path.suffix}"
        )
        backup_path.write_bytes(output_path.read_bytes())
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(OUTPUT_HEADERS)
        for row in rows:
            writer.writerow([row.symbol, row.buy_amount, row.ignore, row.level, row.nick, row.max_amt])
    temp_path.replace(output_path)


def _collect_iwencai_records(args: argparse.Namespace) -> list[dict[str, Any]]:
    import pandas as pd
    import pywencai

    source_sets, final_expression = read_source_sets_from_config(Path(args.source_config))
    if final_expression and not (
        isinstance(final_expression, dict)
        and list(final_expression.keys()) == ["union"]
        and all(isinstance(item, str) for item in final_expression.get("union", []))
    ):
        raise RuntimeError("standalone jingjiabuy output only supports direct union of WenCai source sets")
    selected_names = (
        list(final_expression.get("union", []))
        if isinstance(final_expression, dict) and isinstance(final_expression.get("union"), list)
        else list(source_sets.keys())
    )
    queries = [source_sets[name] for name in selected_names if name in source_sets and source_sets[name].source == "iwencai"]
    if not queries:
        raise RuntimeError(f"no enabled WenCai source sets found in {args.source_config}")

    cookie = resolve_iwencai_cookie(str(args.iwencai_cookie or ""))
    if not cookie:
        raise RuntimeError("IWENCAI_COOKIE is required for standalone stock pool collection")

    records: list[dict[str, Any]] = []
    source_cache_dir = Path(args.source_cache_dir) / datetime.now().strftime("%Y-%m-%d")
    source_cache_dir.mkdir(parents=True, exist_ok=True)
    for item in queries:
        result = pywencai.get(
            query=item.query,
            query_type=str(args.iwencai_query_type or "stock"),
            loop=not bool(args.no_iwencai_loop),
            cookie=cookie,
        )
        df = result if isinstance(result, pd.DataFrame) else pd.DataFrame(result)
        cache_path = source_cache_dir / f"{item.name}.raw.csv"
        df.to_csv(cache_path, index=False, encoding="utf-8-sig")
        if not df.empty:
            records.extend(df.to_dict(orient="records"))
        print(f"STANDALONE_STOCK_POOL source={item.name} rows={len(df)} cache={cache_path}", flush=True)
    return records


def build_standalone_stock_pool(args: argparse.Namespace) -> int:
    records = _collect_iwencai_records(args)
    rows = rows_from_records(records, buy_amount=int(args.buy_amount), ignore=int(args.ignore))
    write_jingjiabuy(rows, Path(args.output), backup_existing=not bool(args.no_backup))

    trace_dir = Path(args.trace_dir) / datetime.now().strftime("%Y-%m-%d") / datetime.now().strftime("%H%M%S")
    trace_dir.mkdir(parents=True, exist_ok=True)
    write_jingjiabuy(rows, trace_dir / "jingjiabuy.csv", backup_existing=False)
    (trace_dir / "manifest.json").write_text(
        json.dumps(
            {
                "output": str(args.output),
                "source_config": str(args.source_config),
                "source_cache_dir": str(args.source_cache_dir),
                "count": len(rows),
                "format": OUTPUT_HEADERS,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"STANDALONE_STOCK_POOL generated output={args.output} stocks={len(rows)} trace_dir={trace_dir}", flush=True)
    return len(rows)


def main() -> None:
    args = build_parser().parse_args()
    build_standalone_stock_pool(args)


if __name__ == "__main__":
    main()
