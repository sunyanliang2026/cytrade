from datetime import date

from config.enums import OrderDirection, OrderStatus
from strategies.overnight_limit_up_buy.strategy import OvernightLimitUpBuyStrategy, calculate_order_quantity
from strategy.models import StrategyConfig
from trading.models import Order


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


class _RejectingExecutor(_FakeExecutor):
    def buy_limit(self, strategy_id, strategy_name, stock_code, price, quantity, remark=""):
        order = super().buy_limit(strategy_id, strategy_name, stock_code, price, quantity, remark)
        order.status = OrderStatus.JUNK
        order.status_msg = "insufficient_cash"
        return order


class _MemoryStore:
    def __init__(self):
        self.records = {}

    def was_submitted(self, trade_day, stock_code):
        return (trade_day, stock_code) in self.records

    def record(self, trade_day, stock_code, payload):
        self.records[(trade_day, stock_code)] = payload


def _strategy(amount=26000, executor=None):
    strategy = OvernightLimitUpBuyStrategy(
        StrategyConfig(stock_code="603005", params={"amount": amount}),
        executor or _FakeExecutor(),
    )
    strategy.start()
    return strategy


def test_calculate_order_quantity_rounds_down_to_lots():
    assert calculate_order_quantity(26000, 11.03) == 2300
    assert calculate_order_quantity(1000, 11.03) == 0


def test_submit_once_uses_limit_up_price_and_records_state():
    executor = _FakeExecutor()
    store = _MemoryStore()
    strategy = _strategy(executor=executor)

    result = strategy.submit_once(
        trade_day=date(2026, 9, 7),
        price_lookup=lambda code: 11.03,
        submission_store=store,
    )

    assert result.status == "submitted"
    assert result.quantity == 2300
    assert executor.orders[0].stock_code == "603005"
    assert executor.orders[0].price == 11.03
    assert executor.orders[0].quantity == 2300
    assert ("2026-09-07", "stock:603005") in store.records


def test_submit_once_skips_when_limit_up_price_missing():
    strategy = _strategy()

    result = strategy.submit_once(trade_day="2026-09-07", price_lookup=lambda code: 0.0)

    assert result.status == "skipped"
    assert result.reason == "limit_up_price_unavailable"


def test_submit_once_skips_duplicate_state_file_record():
    store = _MemoryStore()
    store.record("2026-09-07", "stock:603005", {})
    strategy = _strategy()

    result = strategy.submit_once(
        trade_day="2026-09-07",
        price_lookup=lambda code: 11.03,
        submission_store=store,
    )

    assert result.status == "skipped"
    assert result.reason == "already_submitted_state_file"


def test_submit_once_does_not_record_rejected_order():
    store = _MemoryStore()
    strategy = _strategy(executor=_RejectingExecutor())

    result = strategy.submit_once(
        trade_day="2026-09-07",
        price_lookup=lambda code: 11.03,
        submission_store=store,
    )

    assert result.status == "rejected"
    assert store.records == {}
