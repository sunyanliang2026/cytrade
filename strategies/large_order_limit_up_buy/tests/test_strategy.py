from datetime import datetime

from core.l2_models import L2OrderEvent, L2QuoteEvent
from strategy.models import StrategyConfig
from trading.models import Order
from strategies.large_order_limit_up_buy.strategy import LargeOrderLimitUpBuyStrategy
import strategies.large_order_limit_up_buy.strategy as strategy_module


def event(code="600001", price=8.0, volume=500100, side="BUY", no="1", event_time=None):
    return L2OrderEvent(
        stock_code=code,
        price=price,
        volume=volume,
        amount=price * volume,
        side=side,
        entrust_no=no,
        event_time=event_time or datetime(2026, 9, 7, 10, 0, 0),
    )


def make_strategy(tmp_path, *, code="600001", amount=100000):
    return LargeOrderLimitUpBuyStrategy(
        StrategyConfig(
            stock_code=code,
            params={
                "stock_name": "测试",
                "plan_amount": amount,
                "record_dir": str(tmp_path),
            },
        )
    )


def test_select_stocks_accepts_minimal_manual_pool_without_name(tmp_path):
    pool = tmp_path / "manual_pool.csv"
    pool.write_text("code,plan_amount\n600001,50000\n000001,80000\n", encoding="utf-8")
    strategy = LargeOrderLimitUpBuyStrategy(
        StrategyConfig(stock_code="", params={"csv_path": str(pool), "record_dir": str(tmp_path)})
    )

    stocks = strategy.select_stocks()

    assert [item.stock_code for item in stocks] == ["600001", "000001"]
    assert [item.params["plan_amount"] for item in stocks] == [50000.0, 80000.0]
    assert [item.params["stock_name"] for item in stocks] == ["", ""]


def test_low_price_threshold_uses_lots_and_no_volume_cap(tmp_path):
    strategy = make_strategy(tmp_path)
    assert strategy._is_big_order(7.99, 500100, 7.99 * 500100)
    assert not strategy._is_big_order(7.99, 500000, 7.99 * 500000)
    assert strategy._is_big_order(2.0, 1_500_000, 3_000_000)


def test_normal_price_threshold_uses_amount(tmp_path):
    strategy = make_strategy(tmp_path)
    assert strategy._is_big_order(8.0, 62500, 5_000_001)
    assert not strategy._is_big_order(8.0, 62500, 5_000_000)


def test_only_limit_up_buy_orders_can_trigger(tmp_path):
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", limit_up_price=8.0, pre_close=7.27))
    strategy.on_l2_order(event(price=7.99, volume=600000, no="not-limit"))
    strategy.on_l2_order(event(price=8.0, volume=600000, side="SELL", no="sell"))
    assert strategy._trigger_count == 0


def test_auction_orders_are_recorded_but_do_not_trigger(tmp_path):
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", limit_up_price=8.0, pre_close=7.27))

    strategy.on_l2_order(event(price=8.0, volume=625001, no="auction", event_time=datetime(2026, 9, 8, 9, 15)))

    assert strategy._trigger_count == 0
    assert len(strategy._queue_orders) == 0


def test_continuous_session_orders_can_trigger_at_0930(tmp_path):
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", limit_up_price=8.0, pre_close=7.27, last_price=7.9))

    strategy.on_l2_order(event(price=8.0, volume=625001, no="continuous", event_time=datetime(2026, 9, 8, 9, 30)))

    assert strategy._trigger_count == 1


def test_quote_without_limit_up_uses_exact_qmt_price(monkeypatch, tmp_path):
    class FakeProvider:
        def get_exact_limit_up_price(self, stock_code):
            return 8.0

    monkeypatch.setattr(strategy_module, "LimitUpPriceProvider", FakeProvider)
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", pre_close=7.27, last_price=7.9))
    strategy.on_l2_order(event(price=8.0, volume=625001, no="exact-price"))
    assert strategy._limit_up_price == 8.0
    assert strategy._trigger_count == 1


def test_quote_without_exact_limit_up_does_not_infer_price(monkeypatch, tmp_path):
    class FakeProvider:
        def get_exact_limit_up_price(self, stock_code):
            return 0.0

    monkeypatch.setattr(strategy_module, "LimitUpPriceProvider", FakeProvider)
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", pre_close=7.27))
    strategy.on_l2_order(event(price=8.0, volume=625001, no="no-exact-price"))
    assert strategy._limit_up_price == 0.0
    assert strategy._trigger_count == 0


def test_duplicate_entrust_no_does_not_repeat(tmp_path):
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", limit_up_price=8.0, pre_close=7.27, last_price=7.9))
    trigger = event(price=8.0, volume=625001, no="same")
    strategy.on_l2_order(trigger)
    strategy.on_l2_order(trigger)
    assert strategy._trigger_count == 1


class FakeExecutor:
    def __init__(self):
        self.orders = []

    def buy_limit(self, strategy_id, strategy_name, stock_code, price, quantity, remark):
        order = Order(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            stock_code=stock_code,
            price=price,
            quantity=quantity,
            remark=remark,
        )
        self.orders.append(order)
        return order


def test_big_order_submits_immediately_and_records_front_queue(tmp_path):
    executor = FakeExecutor()
    strategy = LargeOrderLimitUpBuyStrategy(
        StrategyConfig(
            stock_code="600001",
            params={"stock_name": "测试", "plan_amount": 100000, "record_dir": str(tmp_path)},
        ),
        executor,
        None,
    )
    strategy.start()
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", limit_up_price=8.0, pre_close=7.27, last_price=7.9))
    strategy.on_l2_order(event(price=8.0, volume=100000, no="front"))
    strategy.on_l2_order(event(price=8.0, volume=625001, no="trigger"))

    assert len(executor.orders) == 1
    assert executor.orders[0].price == 8.0
    assert strategy._active_order_uuid == executor.orders[0].order_uuid
    snapshot_files = list(tmp_path.rglob("*.queue_snapshots.csv"))
    neighbor_files = list(tmp_path.rglob("*.queue_neighbors.csv"))
    assert len(snapshot_files) == 1
    assert len(neighbor_files) == 1
    assert "front" in neighbor_files[0].read_text(encoding="utf-8")


def test_startup_sealed_stock_waits_for_reopen_then_reseal(tmp_path):
    executor = FakeExecutor()
    strategy = LargeOrderLimitUpBuyStrategy(
        StrategyConfig(
            stock_code="600001",
            params={"plan_amount": 100000, "record_dir": str(tmp_path)},
        ),
        executor,
        None,
    )
    strategy.start()
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=8.0, bid1=8.0,
    ))
    strategy.on_l2_order(event(price=8.0, volume=625001, no="sealed-old"))
    assert strategy._entry_phase == "WAIT_REOPEN"
    assert strategy._trigger_count == 0

    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=7.9, bid1=7.9,
    ))
    assert strategy._entry_phase == "WAIT_RESEAL"
    strategy.on_l2_order(event(price=8.0, volume=625001, no="reseal-new"))

    assert strategy._trigger_count == 1
    assert len(executor.orders) == 1


def test_empty_initial_quote_does_not_bypass_startup_sealed_check(tmp_path):
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", limit_up_price=8.0))
    assert strategy._entry_phase == "WAIT_INITIAL_QUOTE"

    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", limit_up_price=8.0, last_price=8.0))
    assert strategy._entry_phase == "WAIT_REOPEN"
