"""Build the opening-auction scanner universe from source-level stock caches.

This is an offline pool builder. It does not subscribe market data and does not
place orders. The output is intentionally broader than MainSealFollow's final
pool: every stock found in the configured source caches is included once.
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.pool.collect_main_seal_pool import DEFAULT_SOURCE_CACHE_DIR, read_candidates_trace
from scripts.pool.common import normalize_stock_code

DEFAULT_OUTPUT = Path("data/stock_pools/current/opening_auction_universe.csv")
OUTPUT_HEADERS = ["股票代码", "名称", "来源"]


@dataclass
class UniverseRow:
    code: str
    name: str = ""
    source_names: set[str] = field(default_factory=set)


def resolve_date_label(value: str | None = None, *, now: datetime | None = None) -> str:
    text = str(value or "").strip()
    if text:
        return date.fromisoformat(text).isoformat()
    return (now or datetime.now()).date().isoformat()


def source_cache_day_dir(source_cache_dir: Path, date_label: str) -> Path:
    return Path(source_cache_dir) / date_label


def iter_source_cache_files(day_dir: Path) -> list[Path]:
    if not day_dir.is_dir():
        return []
    return sorted(
        path
        for path in day_dir.glob("*.csv")
        if path.is_file() and (path.stem.startswith("iwencai.") or path.stem.startswith("jiuyangongshe."))
    )


def build_universe_from_cache(day_dir: Path) -> list[UniverseRow]:
    rows_by_code: dict[str, UniverseRow] = {}
    for path in iter_source_cache_files(day_dir):
        source_name = path.stem
        for candidate in read_candidates_trace(path):
            code = normalize_stock_code(candidate.code)
            if not code:
                continue
            row = rows_by_code.get(code)
            if row is None:
                row = UniverseRow(code=code, name=str(candidate.name or ""))
                rows_by_code[code] = row
            elif not row.name and candidate.name:
                row.name = str(candidate.name)
            row.source_names.add(source_name)
    return sorted(rows_by_code.values(), key=lambda item: item.code)


def write_universe(rows: list[UniverseRow], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(OUTPUT_HEADERS)
        for row in rows:
            writer.writerow([row.code, row.name, ";".join(sorted(row.source_names))])
    temp_path.replace(output_path)


def build_opening_auction_universe(args: argparse.Namespace) -> int:
    date_label = resolve_date_label(getattr(args, "date", ""))
    day_dir = source_cache_day_dir(Path(args.source_cache_dir), date_label)
    rows = build_universe_from_cache(day_dir)
    if not rows and bool(getattr(args, "strict", False)):
        raise RuntimeError(f"opening auction universe is empty: {day_dir}")
    write_universe(rows, Path(args.output))
    print(
        f"OPENING_AUCTION_UNIVERSE generated output={args.output} date={date_label} "
        f"sources={len(iter_source_cache_files(day_dir))} stocks={len(rows)}",
        flush=True,
    )
    return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build opening-auction scanner universe from source caches.")
    parser.add_argument("--source-cache-dir", default=str(DEFAULT_SOURCE_CACHE_DIR))
    parser.add_argument("--date", default="", help="Cache date in YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--strict", action="store_true", help="Fail when no universe rows are generated.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        build_opening_auction_universe(args)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
