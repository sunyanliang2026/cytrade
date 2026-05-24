"""Collect raw iWenCai stock-pool candidates.

This source script reads one or more iWenCai query conditions from a file and
writes candidates for inspection. The final tradable pool is still produced by
``scripts/collect_main_seal_pool.py``.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.collect_main_seal_pool import (
    DEFAULT_IWENCAI_QUERY_FILE,
    DEFAULT_SOURCE_CONFIG,
    IwencaiQuery,
    PoolCandidate,
    collect_from_iwencai,
    read_iwencai_queries,
    resolve_iwencai_queries,
    resolve_iwencai_cookie,
)


DEFAULT_OUTPUT = Path("data/iwencai_pool_candidates.csv")


def write_candidates(rows: list[tuple[IwencaiQuery, PoolCandidate]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["来源", "类型", "查询名称", "条件", "股票代码", "股票名称", "实际流通市值", "30日最大振幅"])
        for query_config, item in rows:
            writer.writerow(
                [
                    "iwencai",
                    query_config.type,
                    query_config.name,
                    query_config.query,
                    item.code,
                    item.name,
                    f"{item.float_market_value:.0f}",
                    f"{item.max_amplitude_30d:.2f}",
                ]
            )


def collect_once(args) -> int:
    queries = resolve_iwencai_queries(args)
    cookie = resolve_iwencai_cookie(str(args.cookie or ""))
    rows: list[tuple[IwencaiQuery, PoolCandidate]] = []
    for query_config in queries:
        candidates = collect_from_iwencai(
            query=query_config.query,
            cookie=cookie,
            query_type=str(args.query_type),
            loop=not bool(args.no_loop),
        )
        print(
            f"iwencai type={query_config.type} name={query_config.name!r} "
            f"query={query_config.query!r} stocks={len(candidates)}",
            flush=True,
        )
        rows.extend((query_config, item) for item in candidates)
    write_candidates(rows, Path(args.output))
    print(f"已生成问财候选池: {args.output} rows={len(rows)} queries={len(queries)}", flush=True)
    return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成问财来源股票候选池。")
    parser.add_argument("--source-config", default=str(DEFAULT_SOURCE_CONFIG), help=f"股票池来源配置文件，默认 {DEFAULT_SOURCE_CONFIG}。")
    parser.add_argument("--iwencai-query-file", "--query-file", default="", help=f"问财条件文件；不传则读取 --source-config。兼容旧文件 {DEFAULT_IWENCAI_QUERY_FILE}。")
    parser.add_argument("--iwencai-query", "--query", default="", help="单条问财条件；传入后覆盖配置文件。")
    parser.add_argument("--cookie", default="", help="问财登录 cookie；不传则读取环境变量或本地配置。")
    parser.add_argument("--query-type", default="stock", help="pywencai query_type，默认 stock。")
    parser.add_argument("--no-loop", action="store_true", help="pywencai 不自动翻页。")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help=f"输出候选池路径，默认 {DEFAULT_OUTPUT}。")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        collect_once(args)
    except RuntimeError as exc:
        raise SystemExit(f"问财候选池生成失败: {exc}") from None


if __name__ == "__main__":
    main()
