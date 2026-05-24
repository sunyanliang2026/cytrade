"""Collect raw Jiuyangongshe article stock-pool candidates.

The article parsing rules are intentionally fixed. The final tradable pool is
still produced by ``scripts/collect_main_seal_pool.py``.
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
    DEFAULT_SOURCE_CONFIG,
    DEFAULT_SECTOR,
    PoolCandidate,
    collect_from_jiuyangongshe,
    resolve_jiuyangongshe_config,
)


DEFAULT_OUTPUT = Path("data/jiuyangongshe_pool_candidates.csv")


def write_candidates(candidates: list[PoolCandidate], output: Path, article_url: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["来源", "文章URL", "股票代码", "名称"])
        for item in candidates:
            writer.writerow(["jiuyangongshe", article_url, item.code, item.name])


def collect_once(args) -> int:
    if args.no_resolve_codes and not args.allow_name_only_output:
        raise SystemExit(
            "韭研公社候选池默认需要通过 QMT/xtdata 把名称解析成代码；"
            "仅解析调试时才使用 --allow-name-only-output。"
        )
    jiuyangongshe_config = resolve_jiuyangongshe_config(args)
    candidates, article_url, sections = collect_from_jiuyangongshe(
        user_url=jiuyangongshe_config.user_url,
        article_url=str(args.article_url or ""),
        sector=str(args.sector),
        resolve_codes=not bool(args.no_resolve_codes),
        require_today=jiuyangongshe_config.require_today,
    )
    write_candidates(candidates, Path(args.output), article_url)
    print(
        f"已生成韭研公社候选池: {args.output} article={article_url} sections={len(sections)} rows={len(candidates)}",
        flush=True,
    )
    return len(candidates)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成韭研公社来源股票候选池。")
    parser.add_argument("--source-config", default=str(DEFAULT_SOURCE_CONFIG), help=f"股票池来源配置文件，默认 {DEFAULT_SOURCE_CONFIG}。")
    parser.add_argument("--jiuyangongshe-user-url", default="", help="韭研公社用户页 URL；不传则读取 --source-config。")
    parser.add_argument("--article-url", default="", help="指定韭研公社文章 URL；为空时取用户页最新文章。")
    parser.add_argument("--sector", default=DEFAULT_SECTOR, help=f"QMT 板块名，默认 {DEFAULT_SECTOR}。")
    parser.add_argument("--no-resolve-codes", action="store_true", help="不通过 QMT 证券名解析代码，仅用于调试。")
    parser.add_argument("--allow-name-only-output", action="store_true", help="允许输出名称作为代码，仅用于调试。")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help=f"输出候选池路径，默认 {DEFAULT_OUTPUT}。")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        collect_once(args)
    except RuntimeError as exc:
        raise SystemExit(f"韭研公社候选池生成失败: {exc}") from None


if __name__ == "__main__":
    main()
