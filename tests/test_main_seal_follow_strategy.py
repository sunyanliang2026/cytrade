from pathlib import Path

from config.enums import OrderStatus
from core.l2_models import L2OrderEvent, L2OrderQueueEvent, L2QuoteEvent, L2TransactionEvent
from strategy.main_seal_follow_strategy import MainSealFollowStrategy
from strategy.models import StrategyConfig


class _FakeTradeExecutor:
    def __init__(self):
        self.orders = []

    def buy_limit(self, strategy_id, strategy_name, stock_code, price, quantity, remark=""):
        from trading.models import Order

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


class _FakeCancelableTradeExecutor(_FakeTradeExecutor):
    def __init__(self):
        super().__init__()
        self.canceled = []

    def cancel_order(self, order_uuid, remark=""):
        self.canceled.append((order_uuid, remark))
        for order in self.orders:
            if order.order_uuid == order_uuid:
                order.status = OrderStatus.REPORTED_CANCEL
                order.status_msg = remark
                break
        return True


def test_main_seal_follow_select_stocks_from_csv_supports_chinese_headers_and_wan_amount(tmp_path: Path):
    csv_path = tmp_path / "pool.csv"
    csv_path.write_text(
        (
            "\u8bc1\u5238\u4ee3\u7801,\u540d\u79f0,\u8ba1\u5212\u4e70\u5165\u91d1\u989d\n"
            "000001.SZ,\u5e73\u5b89\u94f6\u884c,2\u4e07\n"
            "600519.SH,\u8d35\u5dde\u8305\u53f0,50000\n"
        ),
        encoding="utf-8",
    )

    strategy = MainSealFollowStrategy(
        StrategyConfig(
            params={
                "csv_path": str(csv_path),
                "dry_run": False,
                "big_amount_min": 3_000_000.0,
            }
        )
    )
    configs = strategy.select_stocks()

    assert [cfg.stock_code for cfg in configs] == ["000001", "600519"]
    assert [cfg.max_position_amount for cfg in configs] == [20000.0, 50000.0]
    assert configs[0].params["instance_key"] == "000001"
    assert configs[0].params["dry_run"] is False
    assert configs[0].params["big_amount_min"] == 3_000_000.0


def test_main_seal_follow_submit_feature_orders_tracks_split_orders():
    trade_executor = _FakeTradeExecutor()
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": False,
            },
        ),
        trade_executor=trade_executor,
    )

    orders = strategy.submit_feature_entry_orders(11.0, trigger_reason="unit-test")

    assert len(orders) == 2
    assert [order.quantity for order in orders] == [100, 100]
    assert strategy._entry_state == strategy.STATE_WAIT_ORDER_ACK
    assert strategy._entry_order_uuids == [order.order_uuid for order in orders]


def test_main_seal_follow_submit_feature_orders_requires_executor_when_live():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": False,
            },
        )
    )

    orders = strategy.submit_feature_entry_orders(11.0, trigger_reason="unit-test")

    assert orders == []
    assert strategy._entry_state == strategy.STATE_WAIT_SIGNAL
    assert strategy._entry_order_uuids == []


def test_main_seal_follow_snapshot_restores_cached_l2_and_order_state():
    trade_executor = _FakeTradeExecutor()
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": False,
                "big_amount_min": 2_000_000.0,
            },
        ),
        trade_executor=trade_executor,
    )

    strategy.on_l2_quote(L2QuoteEvent(stock_code="000001", last_price=10.8, limit_up_price=11.0))
    strategy.on_l2_transaction(
        L2TransactionEvent(
            stock_code="000001",
            price=11.0,
            volume=10_000,
            amount=110_000.0,
            side="BUY",
            event_time=123456,
        )
    )
    strategy.on_l2_order(
        L2OrderEvent(
            stock_code="000001",
            price=11.0,
            volume=200_000,
            amount=2_200_000.0,
            side="BUY",
            entrust_no="E1",
            event_time=123457,
        )
    )
    orders = strategy.submit_feature_entry_orders(11.0, trigger_reason="snapshot")
    for order in orders:
        order.status = OrderStatus.REPORTED
        strategy.on_order_update(order)
    strategy.on_l2_orderqueue(
        L2OrderQueueEvent(
            stock_code="000001",
            price=11.0,
            bid_level_volume=[100, 100, 200_000],
            event_time=123458,
        )
    )

    snapshot = strategy.get_snapshot()

    restored = MainSealFollowStrategy(StrategyConfig(stock_code="000001"))
    restored.restore_from_snapshot(snapshot)

    assert [order.order_uuid for order in orders] == restored._entry_order_uuids
    assert restored.get_pending_order_recovery_ids() == [order.order_uuid for order in orders]
    assert list(restored._recent_trades) == list(strategy._recent_trades)
    assert list(restored._recent_big_limit_buy_orders) == list(strategy._recent_big_limit_buy_orders)
    assert restored._current_queue == [100, 100, 200_000]
    assert restored._entry_queue == [100, 100, 200_000]
    assert restored._front_qty_anchor == 0
    assert restored._entry_state == strategy.STATE_IN_QUEUE


def test_main_seal_follow_auto_triggers_dry_run_entry_from_l2_signals():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": True,
                "big_amount_min": 2_000_000.0,
                "sweep_min_amount": 5_000_000.0,
            },
        )
    )
    now_ms = strategy._now_ms()

    strategy.on_l2_quote(L2QuoteEvent(stock_code="000001", last_price=10.8, limit_up_price=11.0))
    strategy.on_l2_transaction(
        L2TransactionEvent(
            stock_code="000001",
            price=11.0,
            volume=500_000,
            amount=5_500_000.0,
            side="BUY",
            event_time=now_ms - 100,
        )
    )
    strategy.on_l2_order(
        L2OrderEvent(
            stock_code="000001",
            price=11.0,
            volume=200_000,
            amount=2_200_000.0,
            side="BUY",
            entrust_no="E1",
            event_time=now_ms - 50,
        )
    )
    strategy.on_l2_orderqueue(
        L2OrderQueueEvent(
            stock_code="000001",
            price=11.0,
            bid_level_volume=[10_000, 200_000, 150_000],
            event_time=now_ms,
        )
    )

    assert strategy._entry_state == strategy.STATE_DRY_RUN_READY
    assert strategy._target_lots == 2
    assert strategy._feature_split_lots == [1, 1]


def test_main_seal_follow_does_not_trigger_without_recent_big_limit_buy():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": True,
                "big_amount_min": 2_000_000.0,
                "sweep_min_amount": 5_000_000.0,
            },
        )
    )
    now_ms = strategy._now_ms()

    strategy.on_l2_quote(L2QuoteEvent(stock_code="000001", last_price=10.8, limit_up_price=11.0))
    strategy.on_l2_transaction(
        L2TransactionEvent(
            stock_code="000001",
            price=11.0,
            volume=500_000,
            amount=5_500_000.0,
            side="BUY",
            event_time=now_ms - 100,
        )
    )
    strategy.on_l2_orderqueue(
        L2OrderQueueEvent(
            stock_code="000001",
            price=11.0,
            bid_level_volume=[10_000, 200_000, 150_000],
            event_time=now_ms,
        )
    )

    assert strategy._entry_state == strategy.STATE_WAIT_SIGNAL
    assert strategy._entry_order_uuids == []


def test_main_seal_follow_estimates_queue_position_and_cancels_weak_queue():
    trade_executor = _FakeCancelableTradeExecutor()
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": False,
                "big_amount_min": 2_000_000.0,
                "back_big_min_amount": 2_000_000.0,
            },
        ),
        trade_executor=trade_executor,
    )
    strategy._limit_up_price = 11.0

    orders = strategy.submit_feature_entry_orders(11.0, trigger_reason="queue-test")
    for order in orders:
        order.status = OrderStatus.REPORTED
        strategy.on_order_update(order)

    strategy.on_l2_orderqueue(
        L2OrderQueueEvent(
            stock_code="000001",
            price=11.0,
            bid_level_volume=[200_000, 100, 100, 250_000],
            event_time=1_000,
        )
    )

    assert strategy._my_start == 1
    assert strategy._my_end == 2
    assert strategy._position_method == "pattern_contiguous"
    assert strategy._position_confidence == "HIGH"
    assert strategy._entry_state == strategy.STATE_IN_QUEUE

    strategy.on_l2_orderqueue(
        L2OrderQueueEvent(
            stock_code="000001",
            price=11.0,
            bid_level_volume=[100, 100, 100],
            event_time=2_000,
        )
    )

    assert strategy._last_cancel_reason == "front_big_weak_and_back_big_empty"
    assert {item[0] for item in trade_executor.canceled} == {order.order_uuid for order in orders}


def test_main_seal_follow_estimates_queue_position_from_front_anchor_fallback():
    trade_executor = _FakeCancelableTradeExecutor()
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": False,
            },
        ),
        trade_executor=trade_executor,
    )
    strategy._limit_up_price = 11.0
    strategy._current_queue = [200_000, 300_000]

    orders = strategy.submit_feature_entry_orders(11.0, trigger_reason="queue-fallback")
    for order in orders:
        order.status = OrderStatus.REPORTED
        strategy.on_order_update(order)

    strategy.on_l2_orderqueue(
        L2OrderQueueEvent(
            stock_code="000001",
            price=11.0,
            bid_level_volume=[200_000, 300_000, 50, 60],
            event_time=3_000,
        )
    )

    assert strategy._my_start == 2
    assert strategy._my_end == 2
    assert strategy._position_method == "front_qty_anchor"
    assert strategy._position_confidence == "LOW"


def test_main_seal_follow_partial_fill_cancels_remaining_entry_orders():
    trade_executor = _FakeCancelableTradeExecutor()
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": False,
            },
        ),
        trade_executor=trade_executor,
    )

    orders = strategy.submit_feature_entry_orders(11.0, trigger_reason="partial-fill")
    partial_order = orders[0]
    partial_order.status = OrderStatus.PART_SUCC
    partial_order.filled_quantity = 100
    partial_order.filled_amount = 1_100.0

    strategy.on_order_update(partial_order)

    assert strategy._entry_state == strategy.STATE_HAS_POSITION
    assert strategy._last_cancel_reason == "deal_happened_cancel_remaining"
    assert {item[0] for item in trade_executor.canceled} == {order.order_uuid for order in orders}


def test_main_seal_follow_position_danger_cancel_rule():
    trade_executor = _FakeCancelableTradeExecutor()
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": False,
                "big_amount_min": 2_000_000.0,
                "back_big_min_amount": 2_000_000.0,
            },
        ),
        trade_executor=trade_executor,
    )
    strategy._limit_up_price = 11.0

    orders = strategy.submit_feature_entry_orders(11.0, trigger_reason="danger-test")
    for order in orders:
        order.status = OrderStatus.REPORTED

    strategy._my_start = 1
    strategy._my_end = 1
    strategy._entry_queue = [300_000, 100, 200_000]
    strategy._entry_position = strategy._analyze_position(strategy._entry_queue)
    strategy._current_queue = [180_000, 100, 100]

    reason = strategy._analyze_queue_and_maybe_cancel()

    assert reason == "position_danger_and_back_big_empty"
    assert strategy._last_cancel_reason == reason
    assert {item[0] for item in trade_executor.canceled} == {order.order_uuid for order in orders}


def test_main_seal_follow_timeout_cancel_rule():
    trade_executor = _FakeCancelableTradeExecutor()
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": False,
                "big_amount_min": 2_000_000.0,
                "back_big_min_amount": 2_000_000.0,
                "max_queue_ms": 1_000,
            },
        ),
        trade_executor=trade_executor,
    )
    strategy._limit_up_price = 11.0

    orders = strategy.submit_feature_entry_orders(11.0, trigger_reason="timeout-test")
    for order in orders:
        order.status = OrderStatus.REPORTED

    strategy._my_start = 1
    strategy._my_end = 1
    strategy._entry_queue = [300_000, 100, 200_000]
    strategy._entry_position = strategy._analyze_position(strategy._entry_queue)
    strategy._current_queue = [300_000, 100, 100]
    strategy._queue_enter_time_ms = strategy._now_ms() - 2_000

    reason = strategy._analyze_queue_and_maybe_cancel()

    assert reason == "queue_timeout_and_back_big_empty"
    assert strategy._last_cancel_reason == reason
    assert {item[0] for item in trade_executor.canceled} == {order.order_uuid for order in orders}
