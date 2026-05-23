"""Collect and schedule MainSealFollow stock-pool generation.

Default source is local QMT/xtdata full-tick snapshot. The default filter is
aligned with the current manual iWenCai query: main-board, non-ST, pct change
greater than 6% and less than 7%.

Examples:
    python scripts/collect_main_seal_pool.py --once
    python scripts/collect_main_seal_pool.py --schedule-time 09:26
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from core.trading_calendar import is_market_day


DEFAULT_OUTPUT = Path("config/main_seal_follow_pool.csv")
OUTPUT_HEADERS = ["股票代码", "名称", "计划买入金额"]
DEFAULT_SECTOR = "沪深A股"


@dataclass(frozen=True)
class PoolCandidate:
    code: str
    name: str
    pct_change: float
    last_price: float
    pre_close: float
    amount: float


def normalize_stock_code(value: str) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6) if text.isdigit() else ""


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
    pct_min: float,
    pct_max: float,
    include_bounds: bool,
    sector: str,
    chunk_size: int = 500,
) -> list[PoolCandidate]:
    from xtquant import xtdata

    xtdata.download_sector_data()
    stock_list = list(xtdata.get_stock_list_in_sector(sector) or [])
    candidates: list[PoolCandidate] = []

    for chunk in _chunks(stock_list, max(1, int(chunk_size or 500))):
        ticks = xtdata.get_full_tick(chunk) or {}
        for xt_code, tick in ticks.items():
            if not is_main_board_code(xt_code):
                continue
            last_price = float(tick.get("lastPrice", 0.0) or 0.0)
            pre_close = float(tick.get("lastClose", 0.0) or 0.0)
            name = _xt_name(xtdata, xt_code)
            ok, pct = should_include_candidate(
                xt_code=xt_code,
                name=name,
                last_price=last_price,
                pre_close=pre_close,
                pct_min=pct_min,
                pct_max=pct_max,
                include_bounds=include_bounds,
            )
            if not ok:
                continue
            candidates.append(
                PoolCandidate(
                    code=normalize_stock_code(xt_code),
                    name=name,
                    pct_change=pct,
                    last_price=last_price,
                    pre_close=pre_close,
                    amount=float(tick.get("amount", 0.0) or 0.0),
                )
            )

    candidates.sort(key=lambda item: (-item.pct_change, -item.amount, item.code))
    return candidates


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
            writer.writerow([item.code, item.name, float(plan_amount)])
    temp_path.replace(output_path)


def collect_once(args) -> int:
    now = datetime.now()
    if args.market_day_only and not is_market_day(now):
        print(f"跳过股票池生成：{now:%Y-%m-%d} 不是交易日", flush=True)
        return 0

    candidates = collect_from_qmt(
        pct_min=float(args.pct_min),
        pct_max=float(args.pct_max),
        include_bounds=bool(args.include_bounds),
        sector=str(args.sector),
        chunk_size=int(args.chunk_size),
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
            f"pct=({args.pct_min},{args.pct_max}) amount={args.amount:g}"
        ),
        flush=True,
    )
    for item in candidates[:20]:
        print(
            f"ROW code={item.code} name={item.name} pct={item.pct_change:.2f} "
            f"last={item.last_price:.3f} pre={item.pre_close:.3f} amount={item.amount:.0f}",
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
    parser.add_argument("--once", action="store_true", help="只立即执行一次后退出。")
    parser.add_argument("--schedule-time", default="", help="常驻定时执行时间，格式 HH:MM。")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help=f"输出路径，默认 {DEFAULT_OUTPUT}")
    parser.add_argument("--amount", type=float, default=1000.0, help="每只股票计划买入金额，默认 1000。")
    parser.add_argument("--pct-min", type=float, default=6.0, help="涨幅下限，默认 6。")
    parser.add_argument("--pct-max", type=float, default=7.0, help="涨幅上限，默认 7。")
    parser.add_argument("--include-bounds", action="store_true", help="涨幅筛选包含上下边界。")
    parser.add_argument("--sector", default=DEFAULT_SECTOR, help=f"QMT 板块名，默认 {DEFAULT_SECTOR}。")
    parser.add_argument("--max-count", type=int, default=0, help="最多输出多少只，0 表示不限制。")
    parser.add_argument("--chunk-size", type=int, default=500, help="批量读取 full tick 的分块大小。")
    parser.add_argument("--no-backup", action="store_true", help="覆盖输出前不备份旧股票池。")
    parser.add_argument("--no-market-day-check", dest="market_day_only", action="store_false", help="非交易日也执行。")
    parser.set_defaults(market_day_only=True)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.once or not args.schedule_time:
        collect_once(args)
        return
    parse_hhmm(args.schedule_time)
    run_scheduler(args)


if __name__ == "__main__":
    main()
