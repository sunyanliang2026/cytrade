import csv
from pathlib import Path

from scripts.pool.build_standalone_stock_pool import (
    DEFAULT_OUTPUT,
    DEFAULT_SOURCE_CACHE_DIR,
    DEFAULT_SOURCE_CONFIG,
    DEFAULT_TRACE_DIR,
    OUTPUT_HEADERS,
    JingjiaBuyRow,
    market_symbol,
    parse_board_level,
    rows_from_records,
    write_jingjiabuy,
    build_parser,
)


def test_standalone_stock_pool_defaults_are_isolated_from_strategy_paths():
    args = build_parser().parse_args([])

    assert Path(args.source_config) == DEFAULT_SOURCE_CONFIG
    assert Path(args.output) == DEFAULT_OUTPUT
    assert Path(args.output).name == "jingjiabuy.csv"
    assert Path(args.trace_dir) == DEFAULT_TRACE_DIR
    assert Path(args.source_cache_dir) == DEFAULT_SOURCE_CACHE_DIR
    assert args.buy_amount == 31000
    assert args.ignore == 0

    paths = [args.source_config, args.output, args.trace_dir, args.source_cache_dir]
    assert all("strategies/main_seal_follow" not in str(path).replace("\\", "/") for path in paths)
    assert all("strategies/opening_auction_attitude" not in str(path).replace("\\", "/") for path in paths)


def test_standalone_stock_pool_allows_explicit_output_override(tmp_path):
    output = tmp_path / "custom_pool.csv"

    args = build_parser().parse_args(["--output", str(output)])

    assert Path(args.output) == output
    assert Path(args.source_config) == DEFAULT_SOURCE_CONFIG


def test_market_symbol_uses_required_exchange_prefix():
    assert market_symbol("600288") == "SHSE.600288"
    assert market_symbol("SHSE.600288") == "SHSE.600288"
    assert market_symbol("000973.SZ") == "SZSE.000973"
    assert market_symbol("002841") == "SZSE.002841"


def test_parse_board_level_matches_jingjiabuy_rules():
    assert parse_board_level("首板") == 1
    assert parse_board_level("1天1板") == 1
    assert parse_board_level("一字板") == 2
    assert parse_board_level("2连板") == 2
    assert parse_board_level("三天两板") == 2
    assert parse_board_level("3天2板") == 2


def test_rows_from_wencai_records_match_jingjiabuy_format():
    rows = rows_from_records(
        [
            {
                "股票代码": "600288.SH",
                "股票简称": "大恒科技",
                "最近50日单日最高成交额": "18.69913047亿",
                "几天几板": "2天2板",
                "首次涨停时间": "09:34:06",
                "最终涨停时间": "14:51:48",
                "最新涨跌幅": "9.97",
            },
            {
                "股票代码": "SZSE.002841",
                "股票简称": "视源股份",
                "最近50日单日最高成交额": "679794056",
                "几天几板": "首板涨停",
                "首次涨停时间": "09:25:00",
                "最终涨停时间": "09:25:00",
                "最新涨跌幅": "10.01",
            },
            {
                "股票代码": "002497.SZ",
                "股票简称": "雅化集团",
                "最近50日单日最高成交额": "2833455640",
                "几天几板": "首板涨停",
                "首次涨停时间": "09:25:00",
                "最终涨停时间": "09:25:00",
                "最新涨跌幅": "9.99",
            },
            {
                "股票代码": "300001.SZ",
                "股票简称": "创业板样例",
                "最近50日单日最高成交额": "1000",
                "几天几板": "1天1板",
                "首次涨停时间": "09:30:00",
                "最终涨停时间": "09:30:00",
                "最新涨跌幅": "20",
            },
        ]
    )

    assert rows == [
        JingjiaBuyRow("SHSE.600288", 31000, 0, 2, "大恒科技", 1_869_913_047),
        JingjiaBuyRow("SZSE.002841", 31000, 0, 2, "视源股份", 679_794_056),
        JingjiaBuyRow("SZSE.002497", 31000, 0, 2, "雅化集团", 2_833_455_640),
    ]


def test_write_jingjiabuy_uses_sample_header_and_utf8(tmp_path):
    output = tmp_path / "jingjiabuy.csv"
    write_jingjiabuy(
        [JingjiaBuyRow("SHSE.600288", 31000, 0, 1, "大恒科技", 1_869_913_047)],
        output,
        backup_existing=False,
    )

    with output.open("r", encoding="utf-8", newline="") as fp:
        rows = list(csv.reader(fp))

    assert rows == [
        OUTPUT_HEADERS,
        ["SHSE.600288", "31000", "0", "1", "大恒科技", "1869913047"],
    ]
