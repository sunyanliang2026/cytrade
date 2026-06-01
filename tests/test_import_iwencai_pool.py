import csv

import pandas as pd

from scripts.pool.import_iwencai_pool import import_pool


def test_import_iwencai_pool_writes_main_seal_pool_csv(tmp_path):
    source = tmp_path / "iwencai.csv"
    output = tmp_path / "main_seal_follow_pool.csv"
    pd.DataFrame(
        [
            {"股票代码": "000001.SZ", "股票简称": "平安银行"},
            {"股票代码": "600000.SH", "股票简称": "浦发银行"},
            {"股票代码": "300001.SZ", "股票简称": "特锐德"},
            {"股票代码": "000001.SZ", "股票简称": "平安银行"},
        ]
    ).to_csv(source, index=False, encoding="utf-8-sig")

    count = import_pool(source, output, amount=20_000)

    assert count == 2
    with output.open("r", encoding="utf-8-sig", newline="") as fp:
        rows = list(csv.reader(fp))

    assert rows == [
        ["股票代码", "名称", "计划买入金额"],
        ["000001", "平安银行", "20000.0"],
        ["600000", "浦发银行", "20000.0"],
    ]
