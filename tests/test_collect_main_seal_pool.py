import csv

from scripts.collect_main_seal_pool import (
    PoolCandidate,
    is_main_board_code,
    is_non_st_name,
    normalize_stock_code,
    should_include_candidate,
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
        ["001259", "利仁科技", "1000.0"],
    ]
