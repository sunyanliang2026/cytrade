from __future__ import annotations

import json
import os
from pathlib import Path

from scripts.pool.common import (
    DEFAULT_IWENCAI_QUERY,
    DEFAULT_IWENCAI_QUERY_FILE,
    DEFAULT_SOURCE_CONFIG,
    IWENCAI_COOKIE_ENV,
    IWENCAI_COOKIE_ENV as COOKIE_ENV,
    IwencaiQuery,
    LOCAL_RUNTIME_CONFIG_PATH,
    PoolCandidate,
    find_column,
    normalize_stock_code,
    parse_metric_number,
)

IWENCAI_CODE_COLUMNS = ("股票代码", "证券代码", "代码", "code", "stock_code")
IWENCAI_NAME_COLUMNS = ("股票简称", "股票名称", "证券简称", "名称", "name", "stock_name")
IWENCAI_FLOAT_MARKET_VALUE_COLUMNS = ("实际流通市值", "自由流通市值", "流通市值")
IWENCAI_AMPLITUDE_COLUMNS = ("30日最大振幅", "最大振幅")


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
        import pandas as pd
        import pywencai
    except ImportError as exc:
        raise RuntimeError("iWenCai 来源需要安装 pywencai，并确保本机 Node.js 可用。") from exc

    try:
        result = pywencai.get(query=query, query_type=query_type, loop=loop, cookie=cookie)
    except Exception as exc:
        raise RuntimeError(f"调用 pywencai 失败: {exc}") from exc

    if result is None:
        return []
    df = result if isinstance(result, pd.DataFrame) else pd.DataFrame(result)
    if df.empty:
        return []

    columns = [str(column) for column in df.columns]
    code_column = find_column(columns, IWENCAI_CODE_COLUMNS)
    name_column = find_column(columns, IWENCAI_NAME_COLUMNS)
    float_market_value_column = find_column(columns, IWENCAI_FLOAT_MARKET_VALUE_COLUMNS)
    amplitude_column = find_column(columns, IWENCAI_AMPLITUDE_COLUMNS)

    if not code_column:
        raise RuntimeError(f"未能从 pywencai 结果识别股票代码列。当前列: {', '.join(columns)}")

    candidates: list[PoolCandidate] = []
    for _, item in df.iterrows():
        code = normalize_stock_code(item.get(code_column))
        if not code:
            continue
        name = str(item.get(name_column, "") or "").strip() if name_column else ""
        if name.lower() == "nan":
            name = ""
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


def read_iwencai_queries(query_file: Path, fallback_query: str = DEFAULT_IWENCAI_QUERY) -> list[IwencaiQuery]:
    path = Path(query_file)
    if not path.exists():
        return [IwencaiQuery(name="default", type="direct", query=fallback_query)] if fallback_query else []

    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            value = value.get("queries", [])
        if not isinstance(value, list):
            raise RuntimeError(f"问财条件文件格式错误: {path}")
        queries: list[IwencaiQuery] = []
        for index, item in enumerate(value, start=1):
            if isinstance(item, str):
                query = item.strip()
                query_type = "direct"
                name = f"query_{index}"
            elif isinstance(item, dict) and item.get("enabled", True):
                query = str(item.get("query", "") or "").strip()
                query_type = str(item.get("type", "direct") or "direct").strip().lower()
                name = str(item.get("name", "") or f"query_{index}").strip()
            else:
                query = ""
                query_type = "direct"
                name = f"query_{index}"
            if query:
                if query_type not in ("base", "direct", "gated"):
                    raise RuntimeError(f"问财条件 type 只能是 base/direct/gated: {path} #{index}")
                queries.append(IwencaiQuery(name=name, type=query_type, query=query))
        return queries

    queries = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        query = line.strip()
        if not query or query.startswith("#"):
            continue
        queries.append(IwencaiQuery(name=f"query_{index}", type="direct", query=query))
    return queries


def parse_iwencai_query_items(raw_queries: object, *, source_name: str) -> list[IwencaiQuery]:
    if not isinstance(raw_queries, list):
        raise RuntimeError(f"问财条件配置必须是数组: {source_name}")
    queries: list[IwencaiQuery] = []
    for index, item in enumerate(raw_queries, start=1):
        if isinstance(item, str):
            query = item.strip()
            query_type = "direct"
            name = f"query_{index}"
        elif isinstance(item, dict) and item.get("enabled", True):
            query = str(item.get("query", "") or "").strip()
            query_type = str(item.get("type", "direct") or "direct").strip().lower()
            name = str(item.get("name", "") or f"query_{index}").strip()
        else:
            query = ""
            query_type = "direct"
            name = f"query_{index}"
        if not query:
            continue
        if query_type not in ("base", "direct", "gated"):
            raise RuntimeError(f"问财条件 type 只能是 base/direct/gated: {source_name} #{index}")
        queries.append(IwencaiQuery(name=name, type=query_type, query=query))
    return queries


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
    env_cookie = os.environ.get(COOKIE_ENV, "")
    if env_cookie:
        return env_cookie
    value = load_local_runtime_config().get(COOKIE_ENV, "")
    return str(value or "")


def resolve_iwencai_queries(args) -> list[IwencaiQuery]:
    from scripts.pool.source_config import read_iwencai_queries_from_source_config, read_source_sets_from_config

    if getattr(args, "iwencai_query", ""):
        return [IwencaiQuery(name="cli", type="direct", query=str(args.iwencai_query))]
    query_file = str(getattr(args, "iwencai_query_file", "") or "")
    if query_file:
        return read_iwencai_queries(Path(query_file))
    source_config = Path(str(getattr(args, "source_config", DEFAULT_SOURCE_CONFIG) or DEFAULT_SOURCE_CONFIG))
    queries = read_iwencai_queries_from_source_config(source_config)
    if queries:
        return queries
    source_sets, _ = read_source_sets_from_config(source_config)
    set_queries = [
        IwencaiQuery(name=name, type="direct", query=item.query)
        for name, item in source_sets.items()
        if item.source == "iwencai" and item.query
    ]
    if set_queries:
        return set_queries
    return read_iwencai_queries(DEFAULT_IWENCAI_QUERY_FILE)


def collect_from_iwencai_queries(
    *,
    queries: list[IwencaiQuery],
    cookie: str,
    query_type: str = "stock",
    loop: bool = True,
) -> list[PoolCandidate]:
    candidates: list[PoolCandidate] = []
    for item in queries:
        items = collect_from_iwencai(
            query=item.query,
            cookie=cookie,
            query_type=query_type,
            loop=loop,
        )
        print(
            f"已通过 pywencai 解析股票池 type={item.type} name={item.name!r} "
            f"query={item.query!r} stocks={len(items)}",
            flush=True,
        )
        candidates.extend(items)
    return candidates
