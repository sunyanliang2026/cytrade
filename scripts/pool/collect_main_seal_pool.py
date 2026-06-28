"""Collect and schedule MainSealFollow stock-pool generation.

This file is intentionally kept as the stable CLI/import entry. Source-specific
logic lives in sibling modules under ``scripts.pool``.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from core.trading_calendar import is_market_day
from scripts.pool.common import (
    DEFAULT_IWENCAI_QUERY,
    DEFAULT_IWENCAI_QUERY_FILE,
    DEFAULT_OUTPUT,
    DEFAULT_TRACE_DIR,
    DEFAULT_SECTOR,
    DEFAULT_SOURCE_CONFIG,
    IWENCAI_COOKIE_ENV,
    LOCAL_RUNTIME_CONFIG_PATH,
    JiuyangongsheConfig,
    IwencaiQuery,
    PoolCandidate,
    SourceSet,
    find_column,
    format_plan_amount,
    is_limit_up_close,
    is_main_board_code,
    is_non_st_name,
    limit_up_price,
    max_amplitude,
    normalize_column_name,
    normalize_stock_code,
    parse_metric_number,
    pct_change,
    round_price,
    should_include_candidate,
    should_include_limitup_candidate,
)
from scripts.pool.iwencai_source import (
    collect_from_iwencai,
    collect_from_iwencai_queries,
    load_local_runtime_config,
    parse_iwencai_query_items,
    read_iwencai_queries,
    resolve_iwencai_cookie,
    resolve_iwencai_queries,
)
from scripts.pool.jiuyangongshe_source import (
    JIUYANGONGSHE_NODE_LABELS,
    build_qmt_name_code_map,
    collect_from_jiuyangongshe,
    collect_from_jiuyangongshe_nodes,
    extract_jiuyangongshe_article_html,
    extract_jiuyangongshe_node_sections,
    extract_known_stock_names,
    extract_latest_jiuyangongshe_article,
    extract_stock_names_from_sections,
    extract_target_sections,
    normalize_article_plain_text,
    resolve_latest_jiuyangongshe_article_url,
    slice_between,
)
from scripts.pool.merge import (
    candidate_code_set,
    evaluate_candidate_expression,
    filter_candidates_by_base,
    intersect_candidate_sets,
    merge_candidates,
    union_candidate_sets,
    write_pool,
)
from scripts.pool.qmt_source import collect_from_qmt
from scripts.pool.source_config import (
    load_source_config,
    read_iwencai_queries_from_source_config,
    read_source_sets_from_config,
    resolve_jiuyangongshe_config,
)

DEFAULT_SOURCE_CACHE_DIR = Path("data/stock_pools/source_cache")
IWENCAI_COLLECT_CUTOFF_HOUR = 9
JIUYANGONGSHE_READY_HOUR = 8
JIUYANGONGSHE_READY_MINUTE = 30


def _safe_filename(value: str) -> str:
    text = str(value or "").strip()
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in text) or "unknown"


def build_trace_run_dir(args, now: datetime) -> Path:
    base = Path(str(getattr(args, "trace_dir", DEFAULT_TRACE_DIR) or DEFAULT_TRACE_DIR))
    return base / now.strftime("%Y-%m-%d") / now.strftime("%H%M%S")


def write_candidates_trace(candidates: list[PoolCandidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(
            [
                "code",
                "name",
                "pct_change",
                "last_price",
                "pre_close",
                "amount",
                "float_market_value",
                "max_amplitude_30d",
            ]
        )
        for item in candidates:
            writer.writerow(
                [
                    item.code,
                    item.name,
                    f"{item.pct_change:.4f}",
                    f"{item.last_price:.4f}",
                    f"{item.pre_close:.4f}",
                    f"{item.amount:.4f}",
                    f"{item.float_market_value:.4f}",
                    f"{item.max_amplitude_30d:.4f}",
                ]
            )


def read_candidates_trace(path: Path) -> list[PoolCandidate]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        rows: list[PoolCandidate] = []
        for row in reader:
            rows.append(
                PoolCandidate(
                    code=str(row.get("code", "") or ""),
                    name=str(row.get("name", "") or ""),
                    pct_change=float(row.get("pct_change", 0) or 0),
                    last_price=float(row.get("last_price", 0) or 0),
                    pre_close=float(row.get("pre_close", 0) or 0),
                    amount=float(row.get("amount", 0) or 0),
                    float_market_value=float(row.get("float_market_value", 0) or 0),
                    max_amplitude_30d=float(row.get("max_amplitude_30d", 0) or 0),
                )
            )
        return rows


def build_source_cache_dir(args, now: datetime) -> Path:
    base = Path(str(getattr(args, "source_cache_dir", DEFAULT_SOURCE_CACHE_DIR) or DEFAULT_SOURCE_CACHE_DIR))
    return base / now.strftime("%Y-%m-%d")


def source_cache_path(args, now: datetime, source_set_name: str) -> Path:
    return build_source_cache_dir(args, now) / f"{_safe_filename(source_set_name)}.csv"


def write_source_cache(args, now: datetime, source_set_name: str, candidates: list[PoolCandidate]) -> Path:
    path = source_cache_path(args, now, source_set_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    write_candidates_trace(candidates, temp_path)
    temp_path.replace(path)
    return path


def read_iwencai_cache_after_cutoff(
    args,
    now: datetime,
    source_set_name: str,
) -> tuple[list[PoolCandidate], Path, str]:
    """Read the pre-09:00 iWenCai cache, failing closed when it is absent.

    iWenCai query semantics change after the open. If the morning job did not
    run before the cutoff, silently treating a missing cache as an empty set can
    still produce a non-empty but incomplete final pool from other sources. The
    safe default is therefore to stop generation and let the monitor-session
    fallback reuse the previous complete CSV, or skip the run when no fallback
    exists.
    """
    cache_path = source_cache_path(args, now, source_set_name)
    allow_missing = bool(getattr(args, "allow_missing_iwencai_cache_after_cutoff", False))
    allow_empty = bool(getattr(args, "allow_empty_iwencai_cache_after_cutoff", False))
    if not cache_path.is_file():
        if not allow_missing:
            raise RuntimeError(
                "9点后禁止重新采集 iWenCai，且未找到当天盘前缓存；"
                f"set={source_set_name} cache={cache_path}。"
                "为避免生成不完整股票池，本次停止生成；"
                "请确认 9 点前定时任务已运行，或仅在人工确认风险后使用 "
                "--allow-missing-iwencai-cache-after-cutoff。"
            )
        return [], cache_path, "missing_allowed"

    candidates = read_candidates_trace(cache_path)
    if not candidates and not allow_empty:
        raise RuntimeError(
            "9点后 iWenCai 当天盘前缓存为空；"
            f"set={source_set_name} cache={cache_path}。"
            "为避免生成不完整股票池，本次停止生成；"
            "如确认当天该来源确实为空，仅可人工加 "
            "--allow-empty-iwencai-cache-after-cutoff。"
        )
    status = "hit" if candidates else "empty_allowed"
    return candidates, cache_path, status


def should_collect_iwencai(now: datetime) -> bool:
    return now.hour < IWENCAI_COLLECT_CUTOFF_HOUR


def should_collect_jiuyangongshe(now: datetime) -> bool:
    return (now.hour, now.minute) >= (JIUYANGONGSHE_READY_HOUR, JIUYANGONGSHE_READY_MINUTE)


def write_trace_manifest(run_dir: Path, manifest: dict) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def collect_configured_source_sets(args, now: datetime) -> tuple[dict[str, list[PoolCandidate]], object]:
    source_sets, final_expression = read_source_sets_from_config(Path(args.source_config))
    if not source_sets:
        return {}, None

    named_sets: dict[str, list[PoolCandidate]] = {}
    iwencai_sets = [item for item in source_sets.values() if item.source == "iwencai"]
    if should_collect_iwencai(now):
        cookie = resolve_iwencai_cookie(str(args.iwencai_cookie or ""))
        for source_set in iwencai_sets:
            candidates = collect_from_iwencai(
                query=source_set.query,
                cookie=cookie,
                query_type=str(args.iwencai_query_type),
                loop=not bool(args.no_iwencai_loop),
            )
            named_sets[source_set.name] = candidates
            cache_path = write_source_cache(args, now, source_set.name, candidates)
            print(f"SET {source_set.name} source=iwencai raw={len(candidates)} cache={cache_path}", flush=True)
    else:
        for source_set in iwencai_sets:
            candidates, cache_path, cache_status = read_iwencai_cache_after_cutoff(args, now, source_set.name)
            named_sets[source_set.name] = candidates
            print(
                f"SET {source_set.name} source=iwencai reused_cache={cache_path} raw={len(candidates)} "
                f"reason=after_0900 cache_status={cache_status}",
                flush=True,
            )

    jiuyangongshe_sets = [item for item in source_sets.values() if item.source == "jiuyangongshe"]
    if jiuyangongshe_sets and not args.no_jiuyangongshe and should_collect_jiuyangongshe(now):
        jiuyangongshe_config = resolve_jiuyangongshe_config(args)
        if jiuyangongshe_config.enabled:
            try:
                if args.no_resolve_codes and not args.allow_name_only_output:
                    raise RuntimeError(
                        "jiuyangongshe source requires QMT name-code resolution for a tradable pool. "
                        "Use --allow-name-only-output only for parser debugging."
                    )
                node_names = [item.node for item in jiuyangongshe_sets if item.node]
                node_results, article_url, _ = collect_from_jiuyangongshe_nodes(
                    user_url=jiuyangongshe_config.user_url,
                    article_url=str(args.article_url or ""),
                    sector=str(args.sector),
                    nodes=node_names,
                    resolve_codes=not bool(args.no_resolve_codes),
                    require_today=jiuyangongshe_config.require_today,
                )
                for source_set in jiuyangongshe_sets:
                    candidates = node_results.get(source_set.node, [])
                    named_sets[source_set.name] = candidates
                    cache_path = write_source_cache(args, now, source_set.name, candidates)
                    print(
                        f"SET {source_set.name} source=jiuyangongshe node={source_set.node} "
                        f"raw={len(candidates)} article={article_url} cache={cache_path}",
                        flush=True,
                    )
            except RuntimeError as exc:
                if args.strict_sources:
                    raise
                for source_set in jiuyangongshe_sets:
                    named_sets[source_set.name] = []
                print(f"WARNING 跳过韭研公社集合: {exc}", flush=True)
    else:
        for source_set in jiuyangongshe_sets:
            named_sets[source_set.name] = []
    return named_sets, final_expression


def collect_once(args) -> int:
    now = datetime.now()
    trace_run_dir = build_trace_run_dir(args, now)
    trace_sources_dir = trace_run_dir / "sources"
    trace_merge_dir = trace_run_dir / "merge"
    trace_manifest: dict = {
        "generated_at": now.isoformat(timespec="seconds"),
        "source": str(args.source),
        "output": str(args.output),
        "amount": float(args.amount),
        "source_config": str(getattr(args, "source_config", "")),
        "source_cache_dir": str(build_source_cache_dir(args, now)),
        "sources": {},
        "files": {},
    }
    if args.market_day_only and not is_market_day(now):
        print(f"跳过股票池生成：{now:%Y-%m-%d} 不是交易日", flush=True)
        return 0

    candidates: list[PoolCandidate]
    if args.source == "jiuyangongshe":
        jiuyangongshe_config = resolve_jiuyangongshe_config(args)
        if args.no_resolve_codes and not args.allow_name_only_output:
            raise SystemExit(
                "jiuyangongshe source requires QMT name-code resolution for a tradable pool. "
                "Use --allow-name-only-output only for parser debugging."
            )
        candidates, article_url, sections = collect_from_jiuyangongshe(
            user_url=jiuyangongshe_config.user_url,
            article_url=str(args.article_url or ""),
            sector=str(args.sector),
            resolve_codes=not bool(args.no_resolve_codes),
            require_today=jiuyangongshe_config.require_today,
        )
        print(f"已解析韭研公社文章 {article_url} sections={len(sections)} stocks={len(candidates)}", flush=True)
        source_path = trace_sources_dir / "jiuyangongshe.csv"
        write_candidates_trace(candidates, source_path)
        trace_manifest["sources"]["jiuyangongshe"] = {
            "raw": len(candidates),
            "file": str(source_path),
            "article_url": article_url,
            "sections": len(sections),
        }
    elif args.source == "iwencai":
        queries = resolve_iwencai_queries(args)
        candidates = collect_from_iwencai_queries(
            queries=queries,
            cookie=resolve_iwencai_cookie(str(args.iwencai_cookie or "")),
            query_type=str(args.iwencai_query_type),
            loop=not bool(args.no_iwencai_loop),
        )
        print(f"已通过 pywencai 汇总股票池: queries={len(queries)} stocks={len(candidates)}", flush=True)
        source_path = trace_sources_dir / "iwencai.csv"
        write_candidates_trace(candidates, source_path)
        trace_manifest["sources"]["iwencai"] = {
            "raw": len(candidates),
            "file": str(source_path),
            "queries": len(queries),
        }
    elif args.source == "combined":
        named_sets, final_expression = collect_configured_source_sets(args, now)
        if named_sets and final_expression is not None:
            candidates = evaluate_candidate_expression(final_expression, named_sets)
            for name, rows in named_sets.items():
                source_path = trace_sources_dir / f"{_safe_filename(name)}.csv"
                write_candidates_trace(rows, source_path)
                trace_manifest["sources"][name] = {
                    "raw": len(rows),
                    "merged": len(merge_candidates(rows)),
                    "file": str(source_path),
                }
                print(f"SET_SUMMARY {name} merged={len(merge_candidates(rows))}", flush=True)
            trace_manifest["final_expression"] = final_expression
            print(f"FINAL expression_result={len(candidates)}", flush=True)
        else:
            queries = resolve_iwencai_queries(args)
            direct_queries = [item for item in queries if item.type == "direct"]
            candidates = collect_from_iwencai_queries(
                queries=direct_queries,
                cookie=resolve_iwencai_cookie(str(args.iwencai_cookie or "")),
                query_type=str(args.iwencai_query_type),
                loop=not bool(args.no_iwencai_loop),
            )
            print(f"已汇总股票池来源: compatibility_direct_queries={len(queries)} raw_stocks={len(candidates)}", flush=True)
            source_path = trace_sources_dir / "compatibility_iwencai_direct.csv"
            write_candidates_trace(candidates, source_path)
            trace_manifest["sources"]["compatibility_iwencai_direct"] = {
                "raw": len(candidates),
                "file": str(source_path),
                "queries": len(direct_queries),
            }
    else:
        candidates = collect_from_qmt(
            sector=str(args.sector),
            chunk_size=int(args.chunk_size),
            min_float_market_value=float(args.min_float_market_value),
            max_amplitude_threshold=float(args.max_amplitude_30d),
            history_count=int(args.history_count),
            download_history=not bool(args.no_download_history),
        )
        source_path = trace_sources_dir / "qmt.csv"
        write_candidates_trace(candidates, source_path)
        trace_manifest["sources"]["qmt"] = {
            "raw": len(candidates),
            "file": str(source_path),
        }

    before_merge_count = len(candidates)
    raw_path = trace_merge_dir / "raw_before_unified_filter.csv"
    write_candidates_trace(candidates, raw_path)
    trace_manifest["files"]["raw_before_unified_filter"] = str(raw_path)
    candidates = merge_candidates(candidates)
    if not candidates:
        raise RuntimeError("股票池为空：所有来源均无有效候选股票")
    if len(candidates) != before_merge_count:
        print(f"统一过滤去重: raw={before_merge_count} merged={len(candidates)}", flush=True)
    if args.max_count > 0:
        candidates = candidates[: int(args.max_count)]
    write_pool(
        candidates,
        Path(args.output),
        float(args.amount),
        backup_existing=not bool(args.no_backup),
    )
    final_trace_path = trace_merge_dir / "final_pool.csv"
    write_candidates_trace(candidates, final_trace_path)
    trace_manifest["files"]["final_pool_trace"] = str(final_trace_path)
    trace_manifest["raw_count"] = before_merge_count
    trace_manifest["final_count"] = len(candidates)
    write_trace_manifest(trace_run_dir, trace_manifest)
    print(
        f"已生成股票池: {args.output} 股票数={len(candidates)} source={args.source} amount={args.amount:g}",
        flush=True,
    )
    print(f"TRACE stock_pool_run={trace_run_dir}", flush=True)
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
    parser.add_argument("--source", choices=("combined", "iwencai", "qmt", "jiuyangongshe"), default="combined", help="股票池来源，默认 combined。")
    parser.add_argument("--once", action="store_true", help="只立即执行一次后退出。")
    parser.add_argument("--schedule-time", default="", help="常驻定时执行时间，格式 HH:MM。")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help=f"输出路径，默认 {DEFAULT_OUTPUT}")
    parser.add_argument("--trace-dir", default=str(DEFAULT_TRACE_DIR), help=f"筛选过程留痕目录，默认 {DEFAULT_TRACE_DIR}")
    parser.add_argument("--source-cache-dir", default=str(DEFAULT_SOURCE_CACHE_DIR), help=f"source-level cache directory, default {DEFAULT_SOURCE_CACHE_DIR}.")
    parser.add_argument("--allow-missing-iwencai-cache-after-cutoff", action="store_true", help="9:00 后 iWenCai 当天缓存缺失时仍允许继续，仅用于人工调试；默认失败以避免不完整股票池。")
    parser.add_argument("--allow-empty-iwencai-cache-after-cutoff", action="store_true", help="9:00 后 iWenCai 当天缓存为空时仍允许继续，仅在确认当天该来源确实为空时使用。")
    parser.add_argument("--source-config", default=str(DEFAULT_SOURCE_CONFIG), help=f"股票池来源配置文件，默认 {DEFAULT_SOURCE_CONFIG}。")
    parser.add_argument("--amount", type=float, default=50000.0, help="每只股票计划买入金额，默认 50000。")
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
    parser.add_argument("--iwencai-query", default="", help="iWenCai 查询语句；传入后覆盖 --iwencai-query-file。")
    parser.add_argument("--iwencai-query-file", default="", help=f"iWenCai 查询条件文件；不传则读取 --source-config。兼容旧文件 {DEFAULT_IWENCAI_QUERY_FILE}。")
    parser.add_argument(
        "--iwencai-cookie",
        default="",
        help=f"iWenCai 登录 cookie；也可用环境变量 {IWENCAI_COOKIE_ENV} 或 {LOCAL_RUNTIME_CONFIG_PATH}。",
    )
    parser.add_argument("--iwencai-query-type", default="stock", help="pywencai query_type，默认 stock。")
    parser.add_argument("--no-iwencai-loop", action="store_true", help="pywencai 不自动翻页。")
    parser.add_argument("--jiuyangongshe-user-url", default="", help="韭研公社用户页 URL；不传则读取 --source-config。")
    parser.add_argument("--article-url", default="", help="指定韭研公社文章 URL；为空时自动取用户页最新文章。")
    parser.add_argument("--no-jiuyangongshe", action="store_true", help="combined 来源下不叠加韭研公社。")
    parser.add_argument("--strict-sources", action="store_true", help="combined 来源下任一来源失败就退出；默认记录 warning 并继续汇总其他来源。")
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
