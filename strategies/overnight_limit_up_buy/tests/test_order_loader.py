from pathlib import Path

import pytest

from strategies.overnight_limit_up_buy.order_loader import OrderCsvError, load_order_requests


def test_load_order_requests_requires_only_stock_code_and_amount(tmp_path: Path):
    csv_path = tmp_path / "orders.csv"
    csv_path.write_text("stock_code,amount\n603005,26000\n002185,12000.5\n", encoding="utf-8")

    rows = load_order_requests(csv_path)

    assert [row.stock_code for row in rows] == ["603005", "002185"]
    assert rows[0].amount == 26000
    assert rows[1].row_no == 3


def test_load_order_requests_rejects_extra_columns(tmp_path: Path):
    csv_path = tmp_path / "orders.csv"
    csv_path.write_text("stock_code,amount,action\n603005,26000,BUY\n", encoding="utf-8")

    with pytest.raises(OrderCsvError, match="exactly"):
        load_order_requests(csv_path)


def test_load_order_requests_allows_duplicate_codes_as_separate_rows(tmp_path: Path):
    csv_path = tmp_path / "orders.csv"
    csv_path.write_text("stock_code,amount\n603005,26000\n603005,30000\n", encoding="utf-8")

    rows = load_order_requests(csv_path)

    assert [(row.row_no, row.stock_code, row.amount) for row in rows] == [
        (2, "603005", 26000.0),
        (3, "603005", 30000.0),
    ]
