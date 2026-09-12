from datetime import datetime

import pytest

from config.enums import OrderStatus
from core.l2_models import L2OrderEvent, L2QuoteEvent, L2TransactionEvent
from core.models import TickData
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


def mark_open_dip(strategy):
    strategy.on_tick(TickData(
        stock_code=strategy.stock_code,
        open=10.0,
        low=9.8,
        last_price=9.8,
        data_time=datetime(2026, 9, 8, 9, 30),
    ))


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


def test_big_order_threshold_uses_unified_amount(tmp_path):
    strategy = make_strategy(tmp_path)
    assert strategy._is_big_order(2.0, 750000, 1_500_000)
    assert strategy._is_big_order(20.0, 75000, 1_500_000)
    assert not strategy._is_big_order(20.0, 74999, 1_499_980)


def test_only_limit_up_buy_orders_can_trigger(tmp_path):
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", limit_up_price=8.0, pre_close=7.27, bid1=7.9))
    strategy.on_l2_order(event(price=7.99, volume=600000, no="not-limit"))
    strategy.on_l2_order(event(price=8.0, volume=600000, side="SELL", no="sell"))
    assert strategy._trigger_count == 0


def test_auction_orders_are_recorded_but_do_not_trigger(tmp_path):
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", limit_up_price=8.0, pre_close=7.27, bid1=7.9))

    strategy.on_l2_order(event(price=8.0, volume=625001, no="auction", event_time=datetime(2026, 9, 8, 9, 15)))

    assert strategy._trigger_count == 0
    assert len(strategy._queue_orders) == 0


def test_continuous_session_orders_can_trigger_at_0930(tmp_path):
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", limit_up_price=8.0, pre_close=7.27, last_price=7.9, bid1=7.9))
    mark_open_dip(strategy)

    strategy.on_l2_order(event(price=8.0, volume=250000, no="continuous", event_time=datetime(2026, 9, 8, 9, 30)))

    assert strategy._trigger_count == 1


def test_quote_without_limit_up_uses_exact_qmt_price(monkeypatch, tmp_path):
    class FakeProvider:
        def get_exact_limit_up_price(self, stock_code):
            return 8.0

    monkeypatch.setattr(strategy_module, "LimitUpPriceProvider", FakeProvider)
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", pre_close=7.27, last_price=7.9, bid1=7.9))
    mark_open_dip(strategy)
    strategy.on_l2_order(event(price=8.0, volume=250000, no="exact-price"))
    assert strategy._limit_up_price == 8.0
    assert strategy._trigger_count == 1


def test_quote_without_exact_limit_up_does_not_infer_price(monkeypatch, tmp_path):
    class FakeProvider:
        def get_exact_limit_up_price(self, stock_code):
            return 0.0

    monkeypatch.setattr(strategy_module, "LimitUpPriceProvider", FakeProvider)
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", pre_close=7.27, bid1=7.9))
    strategy.on_l2_order(event(price=8.0, volume=187500, no="no-exact-price"))
    assert strategy._limit_up_price == 0.0
    assert strategy._trigger_count == 0


def test_duplicate_entrust_no_does_not_repeat(tmp_path):
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", limit_up_price=8.0, pre_close=7.27, last_price=7.9, bid1=7.9))
    mark_open_dip(strategy)
    trigger = event(price=8.0, volume=250000, no="same")
    strategy.on_l2_order(trigger)
    strategy.on_l2_order(trigger)
    assert strategy._trigger_count == 1


class FakeExecutor:
    def __init__(self):
        self.orders = []
        self.cancels = []

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

    def cancel_order(self, order_uuid, remark=""):
        self.cancels.append((order_uuid, remark))
        return True


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
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", limit_up_price=8.0, pre_close=7.27, last_price=7.9, bid1=7.9))
    mark_open_dip(strategy)
    strategy.on_l2_order(event(price=8.0, volume=100000, no="front"))
    strategy.on_l2_order(event(price=8.0, volume=250000, no="trigger"))

    assert len(executor.orders) == 1
    assert executor.orders[0].price == 8.0
    assert strategy._active_order_uuid == executor.orders[0].order_uuid
    snapshot_files = list(tmp_path.rglob("*.queue_snapshots.csv"))
    neighbor_files = list(tmp_path.rglob("*.queue_neighbors.csv"))
    assert len(snapshot_files) == 1
    assert len(neighbor_files) == 1
    assert "front" in neighbor_files[0].read_text(encoding="utf-8")


def test_post_order_30s_writes_one_row_per_big_order_with_fill_and_cancel(tmp_path):
    executor = FakeExecutor()
    strategy = LargeOrderLimitUpBuyStrategy(
        StrategyConfig(stock_code="600001", params={"plan_amount": 100000, "record_dir": str(tmp_path)}),
        executor,
        None,
    )
    strategy.start()
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, bid1=7.9, last_price=7.9,
        event_time=datetime(2026, 9, 8, 9, 30),
    ))
    mark_open_dip(strategy)
    strategy.on_l2_order(event(price=8.0, volume=250000, no="trigger",
                                event_time=datetime(2026, 9, 8, 9, 30)))
    strategy.on_l2_order(event(price=8.0, volume=200000, no="big-1",
                                event_time=datetime(2026, 9, 8, 9, 30, 1)))
    strategy.on_l2_transaction(L2TransactionEvent(
        stock_code="600001", price=8.0, volume=100000, buy_no="big-1",
        event_time=datetime(2026, 9, 8, 9, 30, 2),
    ))
    strategy.on_l2_transaction(L2TransactionEvent(
        stock_code="600001", price=8.0, volume=100000, buy_no="big-1",
        trade_flag=3, event_time=datetime(2026, 9, 8, 9, 30, 3),
    ))
    strategy.on_l2_order(event(price=8.0, volume=100, no="window-end",
                                event_time=datetime(2026, 9, 8, 9, 30, 30)))
    strategy.stop()

    files = list(tmp_path.rglob("*.post_order_30s_orders.csv"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8-sig").splitlines()
    assert lines[0] == "类型,序号,大单时间,委托编号,委托金额,成交金额,撤单金额,剩余金额,状态"
    assert "首封,1," in lines[1]
    assert ",1600000.0,800000.0,800000.0,0.0,成交后撤单" in lines[1]


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
        bid1_volume=130_000, event_time=datetime(2026, 9, 8, 9, 30, 0),
    ))
    strategy.on_l2_order(event(price=8.0, volume=187500, no="sealed-old"))
    assert strategy._entry_phase == "WAIT_REOPEN"
    assert strategy._trigger_count == 0

    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=7.9, bid1=7.9,
        event_time=datetime(2026, 9, 8, 9, 30, 21),
    ))
    assert strategy._entry_phase == "WAIT_RESEAL"
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=7.8, bid1=7.8,
        event_time=datetime(2026, 9, 8, 9, 30, 31),
    ))
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=8.0, bid1=8.0,
        bid1_volume=1000, event_time=datetime(2026, 9, 8, 9, 30, 32),
    ))
    strategy.on_l2_order(event(price=8.0, volume=100, no="reseal-new"))

    assert strategy._trigger_count == 1
    assert len(executor.orders) == 1


def _prepare_reseal_candidate(strategy, *, seal_volume=130_000, break_time=21,
                              reopen_low=7.8, reseal_time=32):
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=8.0, bid1=8.0,
        bid1_volume=seal_volume, event_time=datetime(2026, 9, 8, 9, 30, 0),
    ))
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=7.9, bid1=7.9,
        event_time=datetime(2026, 9, 8, 9, 30, break_time),
    ))
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=reopen_low, bid1=reopen_low,
        event_time=datetime(2026, 9, 8, 9, 30, reseal_time - 1),
    ))
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=8.0, bid1=8.0,
        bid1_volume=1000, event_time=datetime(2026, 9, 8, 9, 30, reseal_time),
    ))


@pytest.mark.parametrize(
    ("seal_volume", "break_time", "reopen_low", "reseal_time"),
    [
        (124_999, 21, 7.8, 32),  # prior seal amount is below 100 million
        (130_000, 20, 7.8, 32),  # prior seal duration is not greater than 20s
        (130_000, 21, 7.8, 30),  # reopen duration is below 10s
        (130_000, 21, 7.9, 32),  # reopen low does not break 98.5%
    ],
)
def test_reseal_requires_each_gate(tmp_path, seal_volume, break_time, reopen_low, reseal_time):
    strategy = make_strategy(tmp_path)
    _prepare_reseal_candidate(
        strategy,
        seal_volume=seal_volume,
        break_time=break_time,
        reopen_low=reopen_low,
        reseal_time=reseal_time,
    )

    strategy.on_l2_order(event(price=8.0, volume=100, no="blocked-reseal"))

    assert strategy._trigger_count == 0
    assert strategy._entry_phase in {"WAIT_REOPEN", "WAIT_RESEAL"}
    assert strategy._reseal_ready is False


def test_unqualified_new_seal_does_not_reuse_old_reopen_window(tmp_path):
    strategy = make_strategy(tmp_path)
    _prepare_reseal_candidate(strategy)
    assert strategy._entry_phase == "WAIT_RESEAL"

    # Reseal briefly, then break again before 20 seconds and below 100m.
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=7.9, bid1=7.9,
        event_time=datetime(2026, 9, 8, 9, 30, 33),
    ))

    assert strategy._entry_phase == "WAIT_REOPEN"
    assert strategy._reopen_since is None
    assert strategy._reseal_ready is False


def test_startup_sealed_status_uses_bid1_not_last_price(tmp_path):
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", limit_up_price=8.0))
    assert strategy._entry_phase == "WAIT_INITIAL_QUOTE"

    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=8.0, bid1=7.9,
    ))
    assert strategy._entry_phase == "READY"


def test_unsealed_start_requires_open_dip_before_trigger(tmp_path):
    strategy = make_strategy(tmp_path)
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=7.9, bid1=7.9,
    ))
    strategy.on_tick(TickData(
        stock_code="600001", open=10.0, low=9.86, last_price=9.86,
        data_time=datetime(2026, 9, 8, 9, 30),
    ))
    strategy.on_l2_order(event(price=8.0, volume=250000, no="no-dip"))
    assert strategy._trigger_count == 0

    strategy.on_tick(TickData(
        stock_code="600001", open=10.0, low=9.84, last_price=9.84,
        data_time=datetime(2026, 9, 8, 9, 31),
    ))
    strategy.on_l2_order(event(price=8.0, volume=250000, no="after-dip"))
    assert strategy._trigger_count == 1


def test_reseal_path_does_not_require_open_dip(tmp_path):
    executor = FakeExecutor()
    strategy = LargeOrderLimitUpBuyStrategy(
        StrategyConfig(stock_code="600001", params={"plan_amount": 100000, "record_dir": str(tmp_path)}),
        executor,
        None,
    )
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=8.0, bid1=8.0,
        bid1_volume=130_000, event_time=datetime(2026, 9, 8, 9, 30, 0),
    ))
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=7.9, bid1=7.9,
        event_time=datetime(2026, 9, 8, 9, 30, 21),
    ))
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=7.8, bid1=7.8,
        event_time=datetime(2026, 9, 8, 9, 30, 31),
    ))
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, last_price=8.0, bid1=8.0,
        bid1_volume=1000, event_time=datetime(2026, 9, 8, 9, 30, 32),
    ))
    strategy.on_l2_order(event(price=8.0, volume=100, no="reseal-no-dip"))
    assert strategy._entry_phase == "WAIT_RESEAL"
    assert strategy._trigger_count == 1


def test_reseal_submits_first_order_then_cancels_when_validation_fails(tmp_path):
    executor = FakeExecutor()
    strategy = LargeOrderLimitUpBuyStrategy(
        StrategyConfig(stock_code="600001", params={"plan_amount": 100000, "record_dir": str(tmp_path)}),
        executor,
        None,
    )
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, bid1=8.0, bid1_volume=130_000,
        event_time=datetime(2026, 9, 8, 9, 30, 0),
    ))
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, bid1=7.9,
        event_time=datetime(2026, 9, 8, 9, 30, 21),
    ))
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, bid1=7.8,
        event_time=datetime(2026, 9, 8, 9, 30, 31),
    ))
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, bid1=8.0,
        bid1_volume=1000, event_time=datetime(2026, 9, 8, 9, 30, 32),
    ))

    strategy.on_l2_order(event(price=8.0, volume=100, no="reseal-first"))
    for number in range(50):
        strategy.on_l2_order(event(price=8.0, volume=100, no=f"small-{number}"))

    assert len(executor.orders) == 1
    assert len(executor.cancels) == 1
    assert strategy._reseal_validation_result == "failed"
    assert strategy._entry_phase == "DONE"


def test_reseal_keeps_order_when_two_big_orders_arrive_within_twenty(tmp_path):
    executor = FakeExecutor()
    strategy = LargeOrderLimitUpBuyStrategy(
        StrategyConfig(stock_code="600001", params={"plan_amount": 100000, "record_dir": str(tmp_path)}),
        executor,
        None,
    )
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, bid1=8.0, bid1_volume=130_000,
        event_time=datetime(2026, 9, 8, 9, 30, 0),
    ))
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, bid1=7.9,
        event_time=datetime(2026, 9, 8, 9, 30, 21),
    ))
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, bid1=7.8,
        event_time=datetime(2026, 9, 8, 9, 30, 31),
    ))
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, bid1=8.0,
        bid1_volume=1000, event_time=datetime(2026, 9, 8, 9, 30, 32),
    ))

    strategy.on_l2_order(event(price=8.0, volume=100, no="reseal-first"))
    for number in range(50):
        volume = 187500 if number in (3, 15) else 100
        strategy.on_l2_order(event(price=8.0, volume=volume, no=f"follow-{number}"))

    assert len(executor.orders) == 1
    assert executor.cancels == []
    assert strategy._reseal_validation_result == "passed"


def test_filled_entry_disables_all_later_entries_for_the_stock(tmp_path):
    executor = FakeExecutor()
    strategy = LargeOrderLimitUpBuyStrategy(
        StrategyConfig(stock_code="600001", params={"plan_amount": 100000, "record_dir": str(tmp_path)}),
        executor,
        None,
    )
    strategy.on_l2_quote(L2QuoteEvent(stock_code="600001", limit_up_price=8.0, bid1=7.9))
    mark_open_dip(strategy)
    strategy.on_l2_order(event(price=8.0, volume=250000, no="first-limit-up"))

    first_order = executor.orders[0]
    first_order.status = OrderStatus.SUCCEEDED
    first_order.filled_quantity = first_order.quantity
    strategy.on_order_update(first_order)

    strategy.on_l2_order(event(price=8.0, volume=250000, no="later-limit-up"))
    assert strategy._entry_phase == "DONE"
    assert strategy._entry_filled is True
    assert len(executor.orders) == 1
