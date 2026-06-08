import csv

from scripts.pool.build_opening_auction_universe import (
    build_opening_auction_universe,
    build_parser,
    build_universe_from_cache,
    resolve_date_label,
)
from scripts.pool.collect_main_seal_pool import write_candidates_trace
from scripts.pool.common import PoolCandidate


def test_build_opening_auction_universe_unions_source_cache(tmp_path):
    day_dir = tmp_path / "source_cache" / "2026-06-08"
    write_candidates_trace(
        [
            PoolCandidate("000001.SZ", "平安银行", 0, 0, 0, 0),
            PoolCandidate("002463", "沪电股份", 0, 0, 0, 0),
        ],
        day_dir / "iwencai.base_strong.csv",
    )
    write_candidates_trace(
        [
            PoolCandidate("002463.SZ", "", 0, 0, 0, 0),
            PoolCandidate("600604.SH", "市北高新", 0, 0, 0, 0),
            PoolCandidate("300001", "特锐德", 0, 0, 0, 0),
        ],
        day_dir / "jiuyangongshe.hot_events.csv",
    )

    rows = build_universe_from_cache(day_dir)

    assert [(row.code, row.name, sorted(row.source_names)) for row in rows] == [
        ("000001", "平安银行", ["iwencai.base_strong"]),
        ("002463", "沪电股份", ["iwencai.base_strong", "jiuyangongshe.hot_events"]),
        ("300001", "特锐德", ["jiuyangongshe.hot_events"]),
        ("600604", "市北高新", ["jiuyangongshe.hot_events"]),
    ]


def test_build_opening_auction_universe_writes_chinese_csv(tmp_path):
    source_cache_dir = tmp_path / "source_cache"
    output = tmp_path / "opening_auction_universe.csv"
    write_candidates_trace(
        [PoolCandidate("600604.SH", "市北高新", 0, 0, 0, 0)],
        source_cache_dir / "2026-06-08" / "iwencai.limitup_direct.csv",
    )
    args = build_parser().parse_args(
        [
            "--source-cache-dir",
            str(source_cache_dir),
            "--date",
            "2026-06-08",
            "--output",
            str(output),
            "--strict",
        ]
    )

    assert build_opening_auction_universe(args) == 1
    with output.open("r", encoding="utf-8-sig", newline="") as fp:
        rows = list(csv.reader(fp))

    assert rows == [
        ["股票代码", "名称", "来源"],
        ["600604", "市北高新", "iwencai.limitup_direct"],
    ]


def test_build_opening_auction_universe_ignores_non_source_cache_files(tmp_path):
    day_dir = tmp_path / "source_cache" / "2026-06-08"
    write_candidates_trace(
        [PoolCandidate("600604.SH", "市北高新", 0, 0, 0, 0)],
        day_dir / "manual.csv",
    )

    assert build_universe_from_cache(day_dir) == []


def test_build_opening_auction_universe_resolves_date_label():
    assert resolve_date_label("2026-06-08") == "2026-06-08"
