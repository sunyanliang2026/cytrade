import csv
import json
import sys
import types

import pandas as pd

from scripts.collect_main_seal_pool import (
    PoolCandidate,
    collect_from_iwencai,
    extract_known_stock_names,
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
    resolve_iwencai_cookie,
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
    assert [(item.code, item.name) for item in candidates] == [("002463", "沪电股份")]
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
