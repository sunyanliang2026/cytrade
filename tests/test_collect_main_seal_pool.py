import csv
import json
import sys
import types
from datetime import datetime

import pandas as pd
import pytest

import scripts.pool.collect_main_seal_pool as pool_module
from scripts.pool.collect_main_seal_pool import (
    PoolCandidate,
    collect_once,
    collect_from_iwencai,
    extract_latest_jiuyangongshe_article,
    filter_candidates_by_base,
    evaluate_candidate_expression,
    extract_known_stock_names,
    extract_jiuyangongshe_node_sections,
    extract_stock_names_from_sections,
    extract_target_sections,
    is_main_board_code,
    is_limit_up_close,
    is_non_st_name,
    limit_up_price,
    max_amplitude,
    normalize_article_plain_text,
    normalize_stock_code,
    parse_metric_number,
    read_iwencai_queries,
    read_iwencai_queries_from_source_config,
    read_source_sets_from_config,
    resolve_iwencai_cookie,
    merge_candidates,
    should_include_candidate,
    should_include_limitup_candidate,
    write_pool,
)


def test_collect_main_seal_pool_filters_main_board_non_st_pct_range():
    assert normalize_stock_code("001259.SZ") == "001259"
    assert normalize_stock_code("600604.SH") == "600604"

    assert is_main_board_code("001259.SZ") is True
    assert is_main_board_code("600604.SH") is True
    assert is_main_board_code("300001.SZ") is False
    assert is_main_board_code("688001.SH") is False
    assert is_main_board_code("830799.BJ") is False

    assert is_non_st_name("平安银行") is True
    assert is_non_st_name("*ST测试") is False
    assert is_non_st_name("退市测试") is False

    ok, pct = should_include_candidate(
        xt_code="001259.SZ",
        name="利仁科技",
        last_price=10.65,
        pre_close=10.0,
        pct_min=6.0,
        pct_max=7.0,
    )
    assert ok is True
    assert round(pct, 2) == 6.5

    ok, _ = should_include_candidate(
        xt_code="300001.SZ",
        name="特锐德",
        last_price=10.65,
        pre_close=10.0,
        pct_min=6.0,
        pct_max=7.0,
    )
    assert ok is False


def test_collect_main_seal_pool_filters_limitup_float_market_value_and_amplitude():
    assert limit_up_price(10.03) == 11.03
    assert is_limit_up_close(11.03, 10.03) is True
    assert round(max_amplitude([11.0, 12.0, 10.8], [10.0, 10.5, 10.2]), 2) == 20.0

    ok, pct = should_include_limitup_candidate(
        xt_code="600604.SH",
        name="市北高新",
        close_price=11.03,
        pre_close=10.03,
        float_market_value=2_000_000_000.0,
        max_amplitude_30d=49.9,
        min_float_market_value=1_900_000_000.0,
        max_amplitude_threshold=50.0,
    )
    assert ok is True
    assert round(pct, 2) == 9.97

    ok, _ = should_include_limitup_candidate(
        xt_code="300001.SZ",
        name="特锐德",
        close_price=11.03,
        pre_close=10.03,
        float_market_value=2_000_000_000.0,
        max_amplitude_30d=49.9,
        min_float_market_value=1_900_000_000.0,
        max_amplitude_threshold=50.0,
    )
    assert ok is False


def test_collect_main_seal_pool_collects_from_iwencai(monkeypatch):
    calls = {}

    def fake_get(**kwargs):
        calls.update(kwargs)
        return pd.DataFrame(
            [
                {"股票代码": "002463.SZ", "股票简称": "沪电股份", "实际流通市值": "1499.44亿", "30日最大振幅": "45.2%"},
                {"股票代码": "300001.SZ", "股票简称": "特锐德", "实际流通市值": "200亿", "30日最大振幅": "30%"},
                {"股票代码": "002463.SZ", "股票简称": "沪电股份", "实际流通市值": "1499.44亿", "30日最大振幅": "45.2%"},
                {"股票代码": "600000.SH", "股票简称": "*ST测试", "实际流通市值": "20亿", "30日最大振幅": "20%"},
            ]
        )

    monkeypatch.setitem(sys.modules, "pywencai", types.SimpleNamespace(get=fake_get))

    candidates = collect_from_iwencai(query="涨停，主板", cookie="x=y", query_type="stock", loop=True)

    assert calls == {"query": "涨停，主板", "query_type": "stock", "loop": True, "cookie": "x=y"}
    assert [(item.code, item.name) for item in candidates] == [
        ("002463", "沪电股份"),
        ("300001", "特锐德"),
        ("002463", "沪电股份"),
        ("600000", "*ST测试"),
    ]
    assert candidates[0].float_market_value == 149_944_000_000.0
    assert candidates[0].max_amplitude_30d == 45.2
    assert parse_metric_number("19亿") == 1_900_000_000.0

    ok, _ = should_include_limitup_candidate(
        xt_code="600604.SH",
        name="市北高新",
        close_price=11.03,
        pre_close=10.03,
        float_market_value=1_900_000_000.0,
        max_amplitude_30d=49.9,
        min_float_market_value=1_900_000_000.0,
        max_amplitude_threshold=50.0,
    )
    assert ok is False

    ok, _ = should_include_limitup_candidate(
        xt_code="600604.SH",
        name="市北高新",
        close_price=11.03,
        pre_close=10.03,
        float_market_value=2_000_000_000.0,
        max_amplitude_30d=50.0,
        min_float_market_value=1_900_000_000.0,
        max_amplitude_threshold=50.0,
    )
    assert ok is False


def test_collect_main_seal_pool_write_pool_creates_backup(tmp_path):
    output = tmp_path / "main_seal_follow_pool.csv"
    output.write_text("old\n", encoding="utf-8")

    write_pool(
        [
            PoolCandidate(
                code="001259",
                name="利仁科技",
                pct_change=6.5,
                last_price=10.65,
                pre_close=10.0,
                amount=123456.0,
            )
        ],
        output,
        plan_amount=1000,
        backup_existing=True,
    )

    backups = list(tmp_path.glob("main_seal_follow_pool.backup_*.csv"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old\n"
    with output.open("r", encoding="utf-8-sig", newline="") as fp:
        rows = list(csv.reader(fp))

    assert rows == [
        ["股票代码", "名称", "计划买入金额"],
        ["001259", "利仁科技", "1000"],
    ]


def test_collect_main_seal_pool_reads_iwencai_cookie_from_local_runtime(tmp_path, monkeypatch):
    config_path = tmp_path / "local_runtime.json"
    config_path.write_text(json.dumps({"IWENCAI_COOKIE": "cookie-from-file"}), encoding="utf-8")
    monkeypatch.delenv("IWENCAI_COOKIE", raising=False)
    monkeypatch.setenv("CYTRADE_LOCAL_SETTINGS_PATH", str(config_path))

    assert resolve_iwencai_cookie("") == "cookie-from-file"
    assert resolve_iwencai_cookie("cookie-from-cli") == "cookie-from-cli"


def test_collect_main_seal_pool_reads_iwencai_queries_from_file(tmp_path):
    query_file = tmp_path / "queries.txt"
    query_file.write_text(
        "\n".join(
            [
                "# comment",
                "涨停，实际流通市值大于19亿，主板",
                "",
                "昨日涨停，非st，主板",
            ]
        ),
        encoding="utf-8",
    )

    queries = read_iwencai_queries(query_file)
    assert [(item.name, item.type, item.query) for item in queries] == [
        ("query_2", "direct", "涨停，实际流通市值大于19亿，主板"),
        ("query_4", "direct", "昨日涨停，非st，主板"),
    ]


def test_collect_main_seal_pool_reads_typed_iwencai_queries_from_json(tmp_path):
    query_file = tmp_path / "queries.json"
    query_file.write_text(
        json.dumps(
            {
                "queries": [
                    {"name": "base-a", "type": "base", "query": "base query"},
                    {"name": "direct-a", "type": "direct", "query": "direct query"},
                    {"name": "gated-a", "type": "gated", "query": "gated query"},
                    {"name": "off", "type": "direct", "query": "off query", "enabled": False},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    queries = read_iwencai_queries(query_file)

    assert [(item.name, item.type, item.query) for item in queries] == [
        ("base-a", "base", "base query"),
        ("direct-a", "direct", "direct query"),
        ("gated-a", "gated", "gated query"),
    ]


def test_collect_main_seal_pool_reads_iwencai_queries_from_source_config(tmp_path):
    config_file = tmp_path / "sources.json"
    config_file.write_text(
        json.dumps(
            {
                "iwencai": {
                    "queries": [
                        {"name": "base-a", "type": "base", "query": "base query"},
                        {"name": "direct-a", "type": "direct", "query": "direct query"},
                    ]
                },
                "jiuyangongshe": {"enabled": True, "require_today": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    queries = read_iwencai_queries_from_source_config(config_file)

    assert [(item.name, item.type, item.query) for item in queries] == [
        ("base-a", "base", "base query"),
        ("direct-a", "direct", "direct query"),
    ]


def test_collect_main_seal_pool_reads_named_source_sets(tmp_path):
    config_file = tmp_path / "sources.json"
    config_file.write_text(
        json.dumps(
            {
                "sets": {
                    "iwencai.base": {"source": "iwencai", "query": "base query"},
                    "jiuyangongshe.hot": {"source": "jiuyangongshe", "node": "hot_events"},
                },
                "final": {"intersect": ["iwencai.base", "jiuyangongshe.hot"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    source_sets, final_expression = read_source_sets_from_config(config_file)

    assert source_sets["iwencai.base"].query == "base query"
    assert source_sets["jiuyangongshe.hot"].node == "hot_events"
    assert final_expression == {"intersect": ["iwencai.base", "jiuyangongshe.hot"]}


def test_collect_main_seal_pool_evaluates_set_expression():
    named_sets = {
        "direct": [PoolCandidate("002463", "沪电股份", 0, 0, 0, 0)],
        "event": [
            PoolCandidate("600604", "市北高新", 0, 0, 0, 0),
            PoolCandidate("000001", "平安银行", 0, 0, 0, 0),
        ],
        "base": [
            PoolCandidate("600604", "市北高新", 0, 0, 0, 0),
            PoolCandidate("600000", "浦发银行", 0, 0, 0, 0),
        ],
    }

    result = evaluate_candidate_expression(
        {"union": ["direct", {"intersect": ["event", "base"]}]},
        named_sets,
    )

    assert [(item.code, item.name) for item in result] == [
        ("002463", "沪电股份"),
        ("600604", "市北高新"),
    ]


def test_collect_main_seal_pool_extracts_latest_jiuyangongshe_article_date():
    page = '''
    <div class="fs13-ash">2026-05-24 07:08:44</div>
    <a href="/a/abc123" target="_blank"><span>5月24日盘前纪要</span></a>
    '''

    article_url, article_date = extract_latest_jiuyangongshe_article(page)

    assert article_url == "https://www.jiuyangongshe.com/a/abc123"
    assert article_date == "2026-05-24"


def test_collect_main_seal_pool_extracts_jiuyangongshe_nodes_by_id():
    text = """
No.1
盘前热点事件
一、昨日热点 量子科技：国盾量子
No.2
公告精选 一、日常公告 沪电股份：公告内容 二、停复牌
No.3
全球市场
No.4
连板梯队和涨停事件 一、连板梯队 三、涨停事件 市北高新：涨停原因
No.5
机构席位
"""

    nodes = extract_jiuyangongshe_node_sections(text)

    assert set(nodes) == {"hot_events", "daily_announcements", "limit_events"}
    assert "国盾量子" in nodes["hot_events"][1]
    assert "沪电股份" in nodes["daily_announcements"][1]
    assert "市北高新" in nodes["limit_events"][1]


def test_collect_main_seal_pool_merges_candidates_with_final_filters():
    candidates = [
        PoolCandidate("002463", "沪电股份", 0, 0, 0, 0, float_market_value=10),
        PoolCandidate("002463.SZ", "沪电股份", 0, 0, 0, 0, float_market_value=20),
        PoolCandidate("300001", "特锐德", 0, 0, 0, 0),
        PoolCandidate("600000", "*ST测试", 0, 0, 0, 0),
        PoolCandidate("600604.SH", "市北高新", 0, 0, 0, 0),
    ]

    merged = merge_candidates(candidates)

    assert [(item.code, item.name) for item in merged] == [
        ("002463", "沪电股份"),
        ("600604", "市北高新"),
    ]


def test_collect_main_seal_pool_filters_gated_candidates_by_base():
    candidates = [
        PoolCandidate("002463", "沪电股份", 0, 0, 0, 0),
        PoolCandidate("600604", "市北高新", 0, 0, 0, 0),
    ]

    filtered = filter_candidates_by_base(candidates, {"600604"})

    assert [(item.code, item.name) for item in filtered] == [("600604", "市北高新")]


def test_collect_main_seal_pool_combined_source_merges_final_pool(tmp_path, monkeypatch):
    output = tmp_path / "pool.csv"
    trace_dir = tmp_path / "runs"

    class FixedDatetime(datetime):
        @classmethod
        def now(cls):
            return cls(2026, 6, 8, 8, 50)

    source_config = tmp_path / "sources.json"
    source_config.write_text(
        json.dumps(
            {
                "sets": {
                    "iwencai.base": {"source": "iwencai", "query": "base query"},
                    "iwencai.direct": {"source": "iwencai", "query": "direct query"},
                    "jiuyangongshe.hot": {"source": "jiuyangongshe", "node": "hot_events"},
                },
                "final": {
                    "union": [
                        "iwencai.direct",
                        {"intersect": ["jiuyangongshe.hot", "iwencai.base"]},
                    ]
                },
                "jiuyangongshe": {"enabled": True, "require_today": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_iwencai(**kwargs):
        if kwargs["query"] == "base query":
            return [
                PoolCandidate("600604", "市北高新", 0, 0, 0, 0),
                PoolCandidate("000001", "平安银行", 0, 0, 0, 0),
            ]
        if kwargs["query"] == "direct query":
            return [
                PoolCandidate("002463", "沪电股份", 0, 0, 0, 0),
                PoolCandidate("300001", "特锐德", 0, 0, 0, 0),
            ]
        raise AssertionError(kwargs["query"])

    def fake_jiuyangongshe_nodes(**kwargs):
        return (
            {
                "hot_events": [
                    PoolCandidate("002463.SZ", "沪电股份", 0, 0, 0, 0),
                    PoolCandidate("600604.SH", "市北高新", 0, 0, 0, 0),
                    PoolCandidate("000001.SZ", "平安银行", 0, 0, 0, 0),
                    PoolCandidate("600000.SH", "*ST测试", 0, 0, 0, 0),
                ]
            },
            "https://example.test/a/1",
            {"hot_events": ("section", "body")},
        )

    monkeypatch.setattr(pool_module, "collect_from_iwencai", fake_iwencai)
    monkeypatch.setattr(pool_module, "collect_from_jiuyangongshe_nodes", fake_jiuyangongshe_nodes)
    monkeypatch.setattr(pool_module, "resolve_iwencai_cookie", lambda value="": "cookie")
    monkeypatch.setattr(pool_module, "datetime", FixedDatetime)

    args = pool_module.build_parser().parse_args(
        [
            "--source",
            "combined",
            "--once",
            "--no-market-day-check",
            "--source-config",
            str(source_config),
            "--output",
            str(output),
            "--trace-dir",
            str(trace_dir),
            "--amount",
            "1000",
            "--no-backup",
        ]
    )

    assert collect_once(args) == 3
    with output.open("r", encoding="utf-8-sig", newline="") as fp:
        rows = list(csv.reader(fp))

    assert rows == [
        ["股票代码", "名称", "计划买入金额"],
        ["002463", "沪电股份", "1000"],
        ["600604", "市北高新", "1000"],
        ["000001", "平安银行", "1000"],
    ]

    run_dirs = list(trace_dir.glob("*/*"))
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert (run_dir / "sources" / "iwencai.base.csv").exists()
    assert (run_dir / "sources" / "iwencai.direct.csv").exists()
    assert (run_dir / "sources" / "jiuyangongshe.hot.csv").exists()
    assert (run_dir / "merge" / "raw_before_unified_filter.csv").exists()
    assert (run_dir / "merge" / "final_pool.csv").exists()

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"] == "combined"
    assert manifest["final_count"] == 3
    assert sorted(manifest["sources"]) == ["iwencai.base", "iwencai.direct", "jiuyangongshe.hot"]


def test_collect_main_seal_pool_reuses_iwencai_cache_after_0900(tmp_path, monkeypatch):
    source_config = tmp_path / "sources.json"
    source_config.write_text(
        json.dumps(
            {
                "sets": {
                    "iwencai.base": {"source": "iwencai", "query": "base query"},
                    "jiuyangongshe.hot": {"source": "jiuyangongshe", "node": "hot_events"},
                },
                "final": {"intersect": ["jiuyangongshe.hot", "iwencai.base"]},
                "jiuyangongshe": {"enabled": True, "require_today": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    args = pool_module.build_parser().parse_args(
        [
            "--source",
            "combined",
            "--source-config",
            str(source_config),
            "--source-cache-dir",
            str(tmp_path / "cache"),
            "--no-market-day-check",
        ]
    )
    now = datetime(2026, 6, 8, 9, 15)
    pool_module.write_source_cache(
        args,
        now,
        "iwencai.base",
        [PoolCandidate("600604", "市北高新", 0, 0, 0, 0)],
    )

    def fail_iwencai(**kwargs):
        raise AssertionError("iwencai must not be collected after 09:00")

    def fake_jiuyangongshe_nodes(**kwargs):
        return (
            {"hot_events": [PoolCandidate("600604.SH", "市北高新", 0, 0, 0, 0)]},
            "https://example.test/a/today",
            {"hot_events": ("section", "body")},
        )

    monkeypatch.setattr(pool_module, "collect_from_iwencai", fail_iwencai)
    monkeypatch.setattr(pool_module, "collect_from_jiuyangongshe_nodes", fake_jiuyangongshe_nodes)

    named_sets, final_expression = pool_module.collect_configured_source_sets(args, now)

    assert [item.code for item in named_sets["iwencai.base"]] == ["600604"]
    assert [item.code for item in named_sets["jiuyangongshe.hot"]] == ["600604.SH"]
    assert [item.code for item in evaluate_candidate_expression(final_expression, named_sets)] == ["600604"]


def test_collect_main_seal_pool_fails_after_0900_when_iwencai_cache_missing(tmp_path):
    source_config = tmp_path / "sources.json"
    source_config.write_text(
        json.dumps(
            {
                "sets": {
                    "iwencai.base": {"source": "iwencai", "query": "base query"},
                    "jiuyangongshe.hot": {"source": "jiuyangongshe", "node": "hot_events"},
                },
                "final": {"intersect": ["jiuyangongshe.hot", "iwencai.base"]},
                "jiuyangongshe": {"enabled": True, "require_today": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    args = pool_module.build_parser().parse_args(
        [
            "--source",
            "combined",
            "--source-config",
            str(source_config),
            "--source-cache-dir",
            str(tmp_path / "cache"),
            "--no-market-day-check",
        ]
    )

    with pytest.raises(RuntimeError, match="未找到当天盘前缓存"):
        pool_module.collect_configured_source_sets(args, datetime(2026, 6, 8, 9, 15))


def test_collect_main_seal_pool_fails_after_0900_when_iwencai_cache_empty(tmp_path):
    source_config = tmp_path / "sources.json"
    source_config.write_text(
        json.dumps(
            {
                "sets": {
                    "iwencai.base": {"source": "iwencai", "query": "base query"},
                    "jiuyangongshe.hot": {"source": "jiuyangongshe", "node": "hot_events"},
                },
                "final": {"intersect": ["jiuyangongshe.hot", "iwencai.base"]},
                "jiuyangongshe": {"enabled": True, "require_today": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    args = pool_module.build_parser().parse_args(
        [
            "--source",
            "combined",
            "--source-config",
            str(source_config),
            "--source-cache-dir",
            str(tmp_path / "cache"),
            "--no-market-day-check",
        ]
    )
    now = datetime(2026, 6, 8, 9, 15)
    pool_module.write_source_cache(args, now, "iwencai.base", [])

    with pytest.raises(RuntimeError, match="缓存为空"):
        pool_module.collect_configured_source_sets(args, now)


def test_collect_main_seal_pool_skips_jiuyangongshe_before_0830(tmp_path, monkeypatch):
    source_config = tmp_path / "sources.json"
    source_config.write_text(
        json.dumps(
            {
                "sets": {
                    "iwencai.base": {"source": "iwencai", "query": "base query"},
                    "jiuyangongshe.hot": {"source": "jiuyangongshe", "node": "hot_events"},
                },
                "final": {"union": ["iwencai.base", "jiuyangongshe.hot"]},
                "jiuyangongshe": {"enabled": True, "require_today": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    args = pool_module.build_parser().parse_args(
        [
            "--source",
            "combined",
            "--source-config",
            str(source_config),
            "--source-cache-dir",
            str(tmp_path / "cache"),
            "--no-market-day-check",
        ]
    )

    def fake_iwencai(**kwargs):
        return [PoolCandidate("600604", "市北高新", 0, 0, 0, 0)]

    def fail_jiuyangongshe_nodes(**kwargs):
        raise AssertionError("jiuyangongshe must not be collected before 08:30")

    monkeypatch.setattr(pool_module, "collect_from_iwencai", fake_iwencai)
    monkeypatch.setattr(pool_module, "collect_from_jiuyangongshe_nodes", fail_jiuyangongshe_nodes)
    monkeypatch.setattr(pool_module, "resolve_iwencai_cookie", lambda value="": "cookie")

    named_sets, _ = pool_module.collect_configured_source_sets(args, datetime(2026, 6, 8, 8, 20))

    assert [item.code for item in named_sets["iwencai.base"]] == ["600604"]
    assert named_sets["jiuyangongshe.hot"] == []


def test_collect_main_seal_pool_extracts_target_article_sections_and_stocks():
    text = """
No.1
盘前热点事件
一、昨日热点
商业航天并购：金利华电
自动驾驶：联创电子
量子科技：国盾量子、科大国创、格尔软件
No.2
公告精选
一、日常公告
埃夫特：拟收购盛普股份100%股份
中鼎股份：拟发行可转债
二、停复牌
大普微：复牌
No.3
全球市场
No.4
连板梯队和涨停事件
一、连板梯队
三、涨停事件
1、公告涨停
威龙股份：控股股东拟变更
2、玻璃基板
京东方A、亚世光电
No.5
机构席位
"""
    sections = extract_target_sections(text)
    names = extract_stock_names_from_sections(
        sections,
        known_names={
            "金利华电",
            "联创电子",
            "国盾量子",
            "科大国创",
            "格尔软件",
            "埃夫特",
            "中鼎股份",
            "威龙股份",
            "京东方A",
            "亚世光电",
        },
    )

    assert [name for name, _ in sections] == [
        "No.1 盘前热点事件",
        "No.2 公告精选/一、日常公告",
        "No.4 连板梯队和涨停事件/三、涨停事件",
    ]
    assert names == [
        "金利华电",
        "联创电子",
        "国盾量子",
        "科大国创",
        "格尔软件",
        "埃夫特",
        "中鼎股份",
        "威龙股份",
        "京东方A",
        "亚世光电",
    ]


def test_collect_main_seal_pool_normalizes_article_text_joining_a_suffix():
    html = "<p>京东方</p><p>A</p><p>、南玻</p><p>A</p>"

    text = normalize_article_plain_text(html)

    assert "京东方A" in text
    assert "南玻A" in text


def test_collect_main_seal_pool_extracts_known_names_by_article_order():
    section = "中芯国际采购扩产，晨光股份公告增持；京东方A、南玻A涨停，璞 泰 来、多 氟 多公告。"

    names = extract_known_stock_names(
        section,
        {
            "京东方A",
            "南玻A",
            "南玻",
            "中芯国际",
            "晨光股份",
            "国际采购",
            "璞泰来",
            "多氟多",
        },
    )

    assert names == ["中芯国际", "晨光股份", "京东方A", "南玻A", "璞泰来", "多氟多"]


def test_collect_main_seal_pool_covers_selected_jiuyangongshe_nodes():
    sections = [
        (
            "No.1 盘前热点事件",
            (
                "金利华电、联创电子、京东方A、中马传动、锋龙股份、四环生物、华微电子、"
                "国盾量子、科大国创、格尔软件、吉大正元、神州信息、西部材料、再升科技、"
                "通宇通讯、天银机电、信维通信、德明利、兆易创新、佰维存储、胜宏科技、"
                "沪电股份、风华高科、三环集团、兴森科技、润建股份、利通电子、中嘉博创、"
                "协创数据、黄河旋风、四方达、沃尔德、力量钻石、飞力达、新宁物流、"
                "中国船舶、青龙管业、韩建河山、招商南油、中远海能、成飞集成"
                "六、行业要闻"
                "龙蟠科技、华胜天成、通富微电、信测标准、宜通世纪、合百集团、"
                "四方精创、御银股份、南网数字、南网科技、北方稀土、九菱科技"
            ),
        ),
        (
            "No.2 公告精选/一、日常公告",
            "埃夫特、珀莱雅、南京医药、福莱蒽特、宁波华翔、中鼎股份、中芯国际、晨光股份",
        ),
        (
            "No.4 连板梯队和涨停事件/三、涨停事件",
            (
                "威龙股份、金利华电、龙星科技、四环生物、华微电子、派林生物、京东方A、"
                "亚世光电、南玻A、纬达光电、华映科技、龙腾光电、联创电子、索菱股份、"
                "浙江世宝、德赛西威、中马传动、锋龙股份、北投科技"
            ),
        ),
    ]
    expected = [
        "金利华电",
        "联创电子",
        "京东方A",
        "中马传动",
        "锋龙股份",
        "四环生物",
        "华微电子",
        "国盾量子",
        "科大国创",
        "格尔软件",
        "吉大正元",
        "神州信息",
        "西部材料",
        "再升科技",
        "通宇通讯",
        "天银机电",
        "信维通信",
        "德明利",
        "兆易创新",
        "佰维存储",
        "胜宏科技",
        "沪电股份",
        "风华高科",
        "三环集团",
        "兴森科技",
        "润建股份",
        "利通电子",
        "中嘉博创",
        "协创数据",
        "黄河旋风",
        "四方达",
        "沃尔德",
        "力量钻石",
        "飞力达",
        "新宁物流",
        "中国船舶",
        "青龙管业",
        "韩建河山",
        "招商南油",
        "中远海能",
        "成飞集成",
        "龙蟠科技",
        "华胜天成",
        "通富微电",
        "信测标准",
        "宜通世纪",
        "四方精创",
        "御银股份",
        "南网数字",
        "南网科技",
        "北方稀土",
        "九菱科技",
        "埃夫特",
        "珀莱雅",
        "南京医药",
        "福莱蒽特",
        "宁波华翔",
        "中鼎股份",
        "中芯国际",
        "晨光股份",
        "威龙股份",
        "龙星科技",
        "派林生物",
        "亚世光电",
        "南玻A",
        "纬达光电",
        "华映科技",
        "龙腾光电",
        "索菱股份",
        "浙江世宝",
        "德赛西威",
        "北投科技",
    ]

    names = extract_stock_names_from_sections(sections, known_names=set(expected))

    assert names == expected
