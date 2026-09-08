from argparse import Namespace
from datetime import datetime
from pathlib import Path

from config.enums import OrderDirection, OrderStatus
from strategies.overnight_limit_up_buy.scripts.run_overnight_limit_up_buy import (
    build_submit_datetime,
    run_session,
)
from trading.models import Order


class _FakePriceProvider:
    def get_limit_up_price(self, stock_code):
        return {"603005": 11.03, "002185": 9.87, "600108": 5.50}.get(stock_code, 0.0)


class _FakeExecutor:
    live_trading_enabled = False

    def __init__(self):
        self.orders = []

    def buy_limit(self, strategy_id, strategy_name, stock_code, price, quantity, remark=""):
        order = Order(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            stock_code=stock_code,
            direction=OrderDirection.BUY,
            price=price,
            quantity=quantity,
            remark=remark,
            status=OrderStatus.WAIT_REPORTING,
        )
        self.orders.append(order)
        return order


def _args(csv_path: Path, state_path: Path):
    return Namespace(
        csv=str(csv_path),
        state_file=str(state_path),
        submit_time="08:30:00",
        no_wait=True,
        market_day_only=False,
        live=False,
        confirm_live=False,
        confirm_stock_code="",
        confirm_amount="",
        confirm_submit_time="",
        require_plan_confirm=False,
        post_submit_wait_sec=0.0,
        full_console=False,
    )


def test_build_submit_datetime_defaults_seconds():
    assert build_submit_datetime(datetime(2026, 9, 7, 7, 0), "08:30") == datetime(2026, 9, 7, 8, 30)


def test_run_session_submits_dry_run_orders_and_records_state(tmp_path: Path):
    csv_path = tmp_path / "orders.csv"
    state_path = tmp_path / "submitted.json"
    csv_path.write_text("stock_code,amount\n603005,26000\n002185,26000\n", encoding="utf-8")
    executor = _FakeExecutor()

    result = run_session(
        _args(csv_path, state_path),
        price_provider=_FakePriceProvider(),
        trade_executor=executor,
        now_provider=lambda: datetime(2026, 9, 7, 8, 31),
    )

    assert result == "completed"
    assert [order.stock_code for order in executor.orders] == ["603005", "002185"]
    assert state_path.is_file()


def test_run_session_aborts_whole_batch_when_any_price_is_unverified(tmp_path: Path):
    csv_path = tmp_path / "orders.csv"
    state_path = tmp_path / "submitted.json"
    csv_path.write_text("stock_code,amount\n603005,26000\n002185,26000\n", encoding="utf-8")
    executor = _FakeExecutor()

    result = run_session(
        _args(csv_path, state_path),
        price_provider=type("Provider", (), {
            "get_limit_up_price": lambda self, code: 11.03 if code == "603005" else 0.0,
        })(),
        trade_executor=executor,
        now_provider=lambda: datetime(2026, 9, 7, 8, 31),
    )

    assert result == "aborted_preflight_failed"
    assert executor.orders == []


def test_run_session_is_idempotent_by_state_file(tmp_path: Path):
    csv_path = tmp_path / "orders.csv"
    state_path = tmp_path / "submitted.json"
    csv_path.write_text("stock_code,amount\n603005,26000\n", encoding="utf-8")

    first_executor = _FakeExecutor()
    second_executor = _FakeExecutor()
    args = _args(csv_path, state_path)

    assert run_session(
        args,
        price_provider=_FakePriceProvider(),
        trade_executor=first_executor,
        now_provider=lambda: datetime(2026, 9, 7, 8, 31),
    ) == "completed"
    assert run_session(
        args,
        price_provider=_FakePriceProvider(),
        trade_executor=second_executor,
        now_provider=lambda: datetime(2026, 9, 7, 8, 32),
    ) == "aborted_preflight_failed"
    assert first_executor.orders
    assert second_executor.orders == []


def test_run_session_submits_each_duplicate_stock_csv_row(tmp_path: Path):
    csv_path = tmp_path / "orders.csv"
    state_path = tmp_path / "submitted.json"
    csv_path.write_text("stock_code,amount\n600108,1000\n600108,1000\n600108,1000\n", encoding="utf-8")
    executor = _FakeExecutor()

    result = run_session(
        _args(csv_path, state_path),
        price_provider=_FakePriceProvider(),
        trade_executor=executor,
        now_provider=lambda: datetime(2026, 9, 7, 8, 31),
    )

    assert result == "completed"
    assert [order.stock_code for order in executor.orders] == ["600108", "600108", "600108"]
    assert [order.quantity for order in executor.orders] == [100, 100, 100]


def test_run_session_live_accepts_exact_confirmations(tmp_path: Path):
    csv_path = tmp_path / "orders.csv"
    state_path = tmp_path / "submitted.json"
    csv_path.write_text("stock_code,amount\n600108,26000\n", encoding="utf-8")
    args = _args(csv_path, state_path)
    args.live = True
    args.confirm_live = True
    args.confirm_stock_code = "600108"
    args.confirm_amount = "26000"
    args.confirm_submit_time = "14:00:00"
    args.submit_time = "14:00:00"
    executor = _FakeExecutor()

    result = run_session(
        args,
        price_provider=_FakePriceProvider(),
        trade_executor=executor,
        now_provider=lambda: datetime(2026, 9, 7, 14, 0),
    )

    assert result == "completed"
    assert len(executor.orders) == 1
    assert executor.orders[0].stock_code == "600108"


def test_run_session_live_does_not_compare_redundant_confirmation_fields(tmp_path: Path):
    csv_path = tmp_path / "orders.csv"
    state_path = tmp_path / "submitted.json"
    csv_path.write_text("stock_code,amount\n600108,26000\n", encoding="utf-8")
    args = _args(csv_path, state_path)
    args.live = True
    args.confirm_live = True
    args.confirm_stock_code = "600108"
    args.confirm_amount = "26000"
    args.confirm_submit_time = "08:30:00"
    args.submit_time = "14:00:00"
    executor = _FakeExecutor()

    assert run_session(
        args,
        price_provider=_FakePriceProvider(),
        trade_executor=executor,
        now_provider=lambda: datetime(2026, 9, 7, 14, 0),
    ) == "completed"
    assert [order.stock_code for order in executor.orders] == ["600108"]


def test_run_session_live_accepts_confirm_orders_for_multiple_rows(tmp_path: Path):
    csv_path = tmp_path / "orders.csv"
    state_path = tmp_path / "submitted.json"
    csv_path.write_text("stock_code,amount\n600108,26000\n603005,5000.00\n", encoding="utf-8")
    args = _args(csv_path, state_path)
    args.live = True
    args.confirm_live = True
    args.confirm_orders = "600108:26000;603005:5000.00"
    args.confirm_submit_time = "14:00"
    args.submit_time = "14:00:00"
    executor = _FakeExecutor()

    result = run_session(
        args,
        price_provider=_FakePriceProvider(),
        trade_executor=executor,
        now_provider=lambda: datetime(2026, 9, 7, 14, 0),
    )

    assert result == "completed"
    assert [order.stock_code for order in executor.orders] == ["600108", "603005"]
