import json
from pathlib import Path

from config.enums import OrderStatus
from core.l2_models import L2OrderEvent, L2OrderQueueEvent, L2QuoteEvent, L2TransactionEvent
from monitor.logger import _SummaryFilter
import strategy.main_seal_follow_strategy as msf_module
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


class _CaptureLogger:
    def __init__(self):
        self.messages = []

    def info(self, message, *args):
        self.messages.append(str(message) % args if args else str(message))

    def warning(self, message, *args):
        self.messages.append(str(message) % args if args else str(message))

    def error(self, message, *args, **kwargs):
        self.messages.append(str(message) % args if args else str(message))


def _extract_msf_events(messages):
    events = []
    for message in messages:
        marker = "MSF_EVENT "
        if marker not in message:
            continue
        events.append(json.loads(message.split(marker, 1)[1]))
    return events


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
    assert strategy._entry_state == strategy.STATE_CHAIN_SUBMITTED
    assert strategy._entry_order_uuids == [order.order_uuid for order in orders]
    assert strategy._probe_order_uuid == orders[0].order_uuid
    assert strategy._main_order_uuids == [orders[1].order_uuid]


def test_main_seal_follow_emits_structured_events_for_entry_submit(monkeypatch):
    capture = _CaptureLogger()
    monkeypatch.setattr(msf_module, "logger", capture)
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "stock_name": "平安银行",
                "plan_amount": 2200.0,
                "dry_run": True,
            },
        )
    )

    orders = strategy.submit_feature_entry_orders(11.0, trigger_reason="unit-test")
    events = _extract_msf_events(capture.messages)
    event_names = [event["event"] for event in events]

    assert len(orders) == 2
    assert "entry_plan_created" in event_names
    assert "dry_run_entry_submitted" in event_names
    submit_event = next(event for event in events if event["event"] == "dry_run_entry_submitted")
    assert submit_event["stock"] == "000001"
    assert submit_event["name"] == "平安银行"
    assert submit_event["state"] == "WAIT_PROBE_FILL"
    assert submit_event["dry_run"] is True
    assert submit_event["metrics"]["order_count"] == 2


def test_summary_filter_keeps_msf_events():
    record = __import__("logging").LogRecord(
        name="cytrade.trade",
        level=20,
        pathname=__file__,
        lineno=1,
        msg='MSF_EVENT {"event":"entry_signal_accepted"}',
        args=(),
        exc_info=None,
    )

    assert _SummaryFilter().filter(record) is True


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
                "queue_vol_unit": "share",
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
    assert restored._probe_order_uuid == orders[0].order_uuid
    assert restored._main_order_uuids == [orders[1].order_uuid]
    assert restored._front_qty_anchor == 0
    assert restored._entry_state == strategy.STATE_WAIT_PROBE_FILL


def test_main_seal_follow_auto_triggers_dry_run_entry_from_l2_signals():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": True,
                "big_amount_min": 2_000_000.0,
                "queue_vol_unit": "share",
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

    assert strategy._entry_state == strategy.STATE_WAIT_PROBE_FILL
    assert strategy._target_lots == 2
    assert strategy._feature_split_lots == [1, 1]
    assert len(strategy._get_active_entry_orders()) == 2


def test_main_seal_follow_skips_quote_trigger_check_when_far_from_limit():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": True,
                "queue_vol_unit": "share",
            },
        )
    )
    strategy._limit_up_price = 11.0
    strategy._current_queue = [100_000, 200_000]

    def _unexpected_main_seal():
        raise AssertionError("far quote should not trigger entry check")

    strategy._main_seal_ok = _unexpected_main_seal

    strategy.on_l2_quote(
        L2QuoteEvent(
            stock_code="000001",
            last_price=10.80,
            bid1=10.79,
            limit_up_price=11.0,
        )
    )


def test_main_seal_follow_enables_detail_l2_only_near_limit():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": True,
                "queue_vol_unit": "share",
                "quote_trigger_near_limit_ticks": 5,
            },
        )
    )

    assert strategy.current_data_kinds() == {"l2quote"}

    strategy.on_l2_quote(
        L2QuoteEvent(
            stock_code="000001",
            last_price=10.80,
            bid1=10.79,
            pre_close=10.0,
            limit_up_price=11.0,
        )
    )

    assert strategy.current_data_kinds() == {"l2quote"}

    strategy.on_l2_quote(
        L2QuoteEvent(
            stock_code="000001",
            last_price=10.96,
            bid1=10.95,
            pre_close=10.0,
            limit_up_price=11.0,
        )
    )

    assert strategy.current_data_kinds() == {"l2quote", "l2transaction", "l2order", "l2orderqueue"}


def test_main_seal_follow_disables_entry_when_plan_cannot_buy_one_lot():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 1000.0,
                "dry_run": True,
                "quote_trigger_near_limit_ticks": 5,
            },
        )
    )

    strategy.on_l2_quote(
        L2QuoteEvent(
            stock_code="000001",
            last_price=10.96,
            bid1=10.95,
            pre_close=10.0,
            limit_up_price=11.0,
        )
    )

    assert strategy._entry_disabled_reason == "planned_amount_below_one_lot"
    assert strategy._target_lots == 0
    assert strategy.current_data_kinds() == {"l2quote"}
    assert strategy.submit_feature_entry_orders(11.0, trigger_reason="unit-test") == []

    strategy._entry_state = strategy.STATE_HAS_POSITION

    assert strategy.current_data_kinds() == {"l2quote"}
    assert strategy._l2_detail_enabled is False


def test_main_seal_follow_does_not_trigger_without_recent_big_limit_buy():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": True,
                "big_amount_min": 2_000_000.0,
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


def test_main_seal_follow_does_not_trigger_on_existing_limit_queue_by_default():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": True,
                "big_amount_min": 2_000_000.0,
                "queue_vol_unit": "share",
                "existing_limit_observe_ms": 0,
            },
        )
    )
    now_ms = strategy._now_ms()

    strategy.on_l2_quote(
        L2QuoteEvent(
            stock_code="000001",
            last_price=11.0,
            bid1=11.0,
            ask1=0.0,
            bid1_volume=10_000,
            limit_up_price=11.0,
            event_time=now_ms,
        )
    )
    strategy.on_l2_orderqueue(
        L2OrderQueueEvent(
            stock_code="000001",
            price=11.0,
            bid_level_volume=[300_000, 10_000, 20_000],
            reported_total_order_count=300,
            observed_queue_count=3,
            is_partial_queue=True,
            event_time=now_ms,
        )
    )

    assert strategy._entry_state == strategy.STATE_WAIT_SIGNAL
    assert strategy._target_lots == 2
    assert strategy._entry_order_uuids == []


def test_main_seal_follow_existing_limit_queue_is_blocked_by_recent_big_cancel():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": True,
                "big_amount_min": 2_000_000.0,
                "queue_vol_unit": "share",
                "allow_existing_limit_queue": True,
                "existing_limit_observe_ms": 0,
                "block_on_recent_big_limit_cancel": True,
            },
        )
    )
    now_ms = strategy._now_ms()

    strategy.on_l2_quote(
        L2QuoteEvent(
            stock_code="000001",
            last_price=11.0,
            bid1=11.0,
            ask1=0.0,
            bid1_volume=10_000,
            limit_up_price=11.0,
            event_time=now_ms,
        )
    )
    strategy.on_l2_order(
        L2OrderEvent(
            stock_code="000001",
            price=11.0,
            volume=200_000,
            amount=2_200_000.0,
            side="BUY",
            entrust_no="E-CANCEL",
            is_cancel=True,
            event_time=now_ms,
        )
    )
    strategy.on_l2_orderqueue(
        L2OrderQueueEvent(
            stock_code="000001",
            price=11.0,
            bid_level_volume=[300_000, 10_000, 20_000],
            reported_total_order_count=300,
            observed_queue_count=3,
            is_partial_queue=True,
            event_time=now_ms,
        )
    )

    assert strategy._entry_state == strategy.STATE_WAIT_SIGNAL
    assert strategy._entry_order_uuids == []


def test_main_seal_follow_probe_wait_timeout_keeps_waiting_by_default():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": True,
                "probe_wait_timeout_ms": 1,
            },
        )
    )

    orders = strategy.submit_feature_entry_orders(11.0, trigger_reason="probe-wait")
    strategy._entry_state = strategy.STATE_WAIT_PROBE_FILL
    strategy._probe_submit_time_ms = strategy._now_ms() - 10_000

    result = strategy._evaluate_active_entry_market("unit-test")

    assert result == ""
    assert strategy._entry_state == strategy.STATE_WAIT_PROBE_FILL
    assert [order.order_uuid for order in strategy._get_active_entry_orders()] == [
        order.order_uuid for order in orders
    ]
    assert strategy._last_cancel_reason == ""


def test_main_seal_follow_dry_run_simulates_probe_fill_and_keeps_main():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": True,
                "big_amount_min": 2_000_000.0,
                "queue_vol_unit": "share",
                "post_probe_keep_ms": 10_000,
            },
        )
    )
    now_ms = strategy._now_ms()

    strategy.on_l2_quote(L2QuoteEvent(stock_code="000001", last_price=10.8, bid1=10.8, limit_up_price=11.0))
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

    strategy.on_l2_transaction(
        L2TransactionEvent(
            stock_code="000001",
            price=11.0,
            volume=400_000,
            amount=4_400_000.0,
            side="BUY",
            event_time=now_ms + 10,
        )
    )

    assert strategy._probe_fill_time_ms > 0
    assert strategy._probe_filled_quantity == 100
    assert strategy._entry_state == strategy.STATE_MAIN_KEEPING
    assert len(strategy._get_active_main_orders()) == 1


def test_main_seal_follow_dry_run_probe_fill_can_cancel_main():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": True,
                "big_amount_min": 2_000_000.0,
                "queue_vol_unit": "share",
                "cancel_to_add_ratio_max": 0.6,
            },
        )
    )
    now_ms = strategy._now_ms()

    strategy.on_l2_quote(L2QuoteEvent(stock_code="000001", last_price=10.8, bid1=10.8, limit_up_price=11.0))
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

    strategy._recent_limit_buy_add_orders.append({"time": now_ms + 1, "amount": 100_000.0})
    strategy._recent_limit_buy_cancel_orders.append({"time": now_ms + 1, "amount": 3_000_000.0})
    strategy.on_l2_transaction(
        L2TransactionEvent(
            stock_code="000001",
            price=11.0,
            volume=400_000,
            amount=4_400_000.0,
            side="BUY",
            event_time=now_ms + 10,
        )
    )

    assert strategy._last_cancel_reason == "confirmed_limit_buy_cancel_gt_add"
    assert strategy._entry_state == strategy.STATE_WAIT_SIGNAL
    assert strategy._get_active_main_orders() == []


def test_main_seal_follow_recent_big_limit_cancel_blocks_entry():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": True,
                "big_amount_min": 2_000_000.0,
                "block_on_recent_big_limit_cancel": True,
                "queue_vol_unit": "share",
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
            event_time=now_ms - 90,
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
            is_cancel=True,
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

    assert strategy._entry_state == strategy.STATE_WAIT_SIGNAL
    assert strategy._entry_order_uuids == []


def test_main_seal_follow_l2_calibration_mode_writes_jsonl_samples(tmp_path: Path):
    calibration_dir = tmp_path / "l2_calibration"
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "stock_name": "平安银行",
                "dry_run": True,
                "l2_calibration_enabled": True,
                "l2_calibration_dir": str(calibration_dir),
                "queue_vol_unit": "share",
            },
        )
    )

    strategy.on_l2_quote(
        L2QuoteEvent(
            stock_code="000001",
            last_price=10.8,
            pre_close=10.0,
            bid1=10.8,
            ask1=10.81,
            limit_up_price=11.0,
            event_time=123456,
            raw_xt_fields={"lastPrice": 10.8, "upLimitPrice": 11.0},
        )
    )

    files = list(calibration_dir.glob("*.jsonl"))
    assert len(files) == 1

    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = __import__("json").loads(lines[0])

    assert payload["event_type"] == "l2quote"
    assert payload["stock_code"] == "000001"
    assert payload["stock_name"] == "平安银行"
    assert payload["normalized"]["limit_up_price"] == 11.0
    assert payload["raw_xt_fields"]["upLimitPrice"] == 11.0
    assert payload["units"]["queue_vol_unit"] == "share"


def test_main_seal_follow_tracks_partial_queue_without_price_fallback_cancel():
    trade_executor = _FakeCancelableTradeExecutor()
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": False,
                "big_amount_min": 2_000_000.0,
                "back_big_min_amount": 2_000_000.0,
                "queue_vol_unit": "share",
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
            price=10.99,
            bid_level_volume=[10, 20, 30],
            reported_total_order_count=120,
            observed_queue_count=3,
            is_partial_queue=True,
            event_time=1_000,
        )
    )

    assert strategy._current_queue == []
    assert strategy._current_queue_observed_count == 3
    assert strategy._current_queue_reported_count == 120
    assert strategy._current_queue_is_partial is True
    assert trade_executor.canceled == []


def test_main_seal_follow_probe_fill_keeps_main_when_market_not_weak():
    trade_executor = _FakeCancelableTradeExecutor()
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": False,
                "queue_vol_unit": "share",
            },
        ),
        trade_executor=trade_executor,
    )
    strategy._limit_up_price = 11.0

    orders = strategy.submit_feature_entry_orders(11.0, trigger_reason="probe-keep")
    for order in orders:
        order.status = OrderStatus.REPORTED
        strategy.on_order_update(order)

    probe_order = orders[0]
    probe_order.status = OrderStatus.PART_SUCC
    probe_order.filled_quantity = 100
    strategy.on_order_update(probe_order)

    assert strategy._entry_state == strategy.STATE_MAIN_KEEPING
    assert trade_executor.canceled == []
    assert strategy._probe_fill_time_ms > 0


def test_main_seal_follow_probe_fill_cancels_only_main_when_cancel_pressure():
    trade_executor = _FakeCancelableTradeExecutor()
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": False,
                "queue_vol_unit": "share",
                "cancel_to_add_ratio_max": 0.6,
            },
        ),
        trade_executor=trade_executor,
    )
    strategy._limit_up_price = 11.0

    orders = strategy.submit_feature_entry_orders(11.0, trigger_reason="probe-cancel")
    for order in orders:
        order.status = OrderStatus.REPORTED
        strategy.on_order_update(order)

    now_ms = strategy._now_ms()
    strategy._recent_limit_buy_add_orders.append({"time": now_ms, "amount": 100_000.0})
    strategy._recent_limit_buy_cancel_orders.append({"time": now_ms, "amount": 300_000.0})

    probe_order = orders[0]
    probe_order.status = OrderStatus.PART_SUCC
    probe_order.filled_quantity = 100
    strategy.on_order_update(probe_order)

    assert strategy._entry_state == strategy.STATE_MAIN_CANCELING
    assert strategy._last_cancel_reason == "confirmed_limit_buy_cancel_gt_add"
    assert trade_executor.canceled == [(orders[1].order_uuid, "MSF cancel main: confirmed_limit_buy_cancel_gt_add")]


def test_main_seal_follow_maps_shenzhen_cancel_transaction_to_limit_buy_cancel():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="001259",
            params={
                "plan_amount": 10_000.0,
                "dry_run": True,
                "big_amount_min": 100_000.0,
            },
        )
    )
    strategy._limit_up_price = 88.58

    strategy.on_l2_order(
        L2OrderEvent(
            stock_code="001259",
            price=88.58,
            volume=2_000,
            amount=177_160.0,
            side="BUY",
            entrust_no="43360644",
            entrust_type=1,
            entrust_direction=1,
            event_time=1_000,
        )
    )
    strategy.on_l2_transaction(
        L2TransactionEvent(
            stock_code="001259",
            price=0.0,
            volume=800,
            amount=0.0,
            buy_no="43360644",
            sell_no="0",
            trade_flag=3,
            event_time=2_000,
        )
    )

    cancel = list(strategy._recent_limit_buy_cancel_orders)[-1]
    assert cancel["price"] == 88.58
    assert cancel["volume"] == 800
    assert cancel["amount"] == 88.58 * 800
    assert cancel["entrust_no"] == "43360644"


def test_main_seal_follow_main_fill_cancels_remaining_main_orders():
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
    partial_order = orders[1]
    partial_order.status = OrderStatus.PART_SUCC
    partial_order.filled_quantity = 100
    partial_order.filled_amount = 1_100.0

    strategy.on_order_update(partial_order)

    assert strategy._entry_state == strategy.STATE_MAIN_CANCELING
    assert strategy._last_cancel_reason == "main_deal_happened_cancel_remaining"
    assert {item[0] for item in trade_executor.canceled} == {orders[1].order_uuid}


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
                "queue_vol_unit": "share",
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
                "queue_vol_unit": "share",
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


def test_main_seal_follow_cooldown_blocks_immediate_reentry_after_cancel():
    strategy = MainSealFollowStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "plan_amount": 2200.0,
                "dry_run": True,
                "big_amount_min": 2_000_000.0,
                "cooldown_ms": 5_000,
                "queue_vol_unit": "share",
            },
        )
    )
    now_ms = strategy._now_ms()
    strategy._last_cancel_time_ms = now_ms - 1_000

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

    assert strategy._entry_state == strategy.STATE_WAIT_SIGNAL
    assert strategy._entry_order_uuids == []
