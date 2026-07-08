from datetime import datetime

import core.data_subscription as data_subscription_module
from core.connection import ConnectionManager
from core.data_subscription import DataSubscriptionManager
from core.l2_models import L2OrderEvent, L2OrderQueueEvent, L2QuoteEvent, L2TransactionEvent
from core.models import TickData
from strategy.base import BaseStrategy
from strategy.models import StrategyConfig
from strategy.runner import StrategyRunner, _select_configs_in_subprocess


class _FakeDataSubscription:
    def __init__(self):
        self.tick_codes = set()
        self.l2_map = {}

    def get_subscription_list(self):
        return list(self.tick_codes)

    def get_l2_subscription_map(self):
        return {code: sorted(kinds) for code, kinds in self.l2_map.items()}

    def subscribe_stocks(self, codes, period=""):
        self.tick_codes.update(codes)

    def unsubscribe_stocks(self, codes):
        for code in codes:
            self.tick_codes.discard(code)

    def subscribe_l2_stocks(self, codes, kinds=None):
        kinds = set(kinds or [])
        for code in codes:
            self.l2_map.setdefault(code, set()).update(kinds)

    def unsubscribe_l2_stocks(self, codes, kinds=None):
        kinds = set(kinds or [])
        for code in codes:
            existing = self.l2_map.get(code, set())
            existing.difference_update(kinds)
            if existing:
                self.l2_map[code] = existing
            else:
                self.l2_map.pop(code, None)

    def set_data_callback(self, callback):
        self.tick_callback = callback

    def set_l2_quote_callback(self, callback):
        self.l2_quote_callback = callback

    def set_l2_transaction_callback(self, callback):
        self.l2_transaction_callback = callback

    def set_l2_order_callback(self, callback):
        self.l2_order_callback = callback

    def set_l2_orderqueue_callback(self, callback):
        self.l2_orderqueue_callback = callback


class _DummyL2Strategy(BaseStrategy):
    strategy_name = "DummyL2Strategy"

    def __init__(self, config: StrategyConfig, trade_executor=None, position_manager=None):
        super().__init__(config, trade_executor, position_manager)
        self.l2_quote_events = []
        self.l2_transaction_events = []
        self.l2_order_events = []
        self.l2_orderqueue_events = []

    @classmethod
    def required_data_kinds(cls) -> set[str]:
        return {"tick", "l2quote", "l2transaction", "l2order", "l2orderqueue"}

    def on_tick(self, tick):
        return None

    def select_stocks(self):
        return []

    def on_l2_quote(self, event):
        self.l2_quote_events.append(event)

    def on_l2_transaction(self, event):
        self.l2_transaction_events.append(event)

    def on_l2_order(self, event):
        self.l2_order_events.append(event)

    def on_l2_orderqueue(self, event):
        self.l2_orderqueue_events.append(event)


class _DummyTickStrategy(BaseStrategy):
    strategy_name = "DummyTickStrategy"

    def __init__(self, config: StrategyConfig, trade_executor=None, position_manager=None):
        super().__init__(config, trade_executor, position_manager)
        self.ticks = []

    def on_tick(self, tick):
        self.ticks.append(tick)
        return None

    def select_stocks(self):
        return []


class _DummyDynamicL2Strategy(BaseStrategy):
    strategy_name = "DummyDynamicL2Strategy"

    def __init__(self, config: StrategyConfig, trade_executor=None, position_manager=None):
        super().__init__(config, trade_executor, position_manager)
        self.detail_enabled = False

    @classmethod
    def required_data_kinds(cls) -> set[str]:
        return {"l2quote"}

    def current_data_kinds(self):
        if self.detail_enabled:
            return {"l2quote", "l2transaction", "l2order", "l2orderqueue"}
        return {"l2quote"}

    def on_l2_quote(self, event):
        if float(event.last_price or 0.0) >= 10.0:
            self.detail_enabled = True

    def on_tick(self, tick):
        return None

    def select_stocks(self):
        return []


class _SettingsAwareSelectionStrategy(BaseStrategy):
    strategy_name = "SettingsAwareSelectionStrategy"

    def on_tick(self, tick):
        return None

    def select_stocks(self):
        from config.settings import settings as global_settings

        csv_path = str(getattr(global_settings, "CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH", "") or "")
        return [
            StrategyConfig(
                stock_code="600162",
                params={
                    "csv_path": csv_path,
                },
            )
        ]

def test_data_subscription_mock_callbacks_cover_tick_and_l2():
    manager = DataSubscriptionManager()
    captured = {}

    manager.set_data_callback(lambda payload: captured.setdefault("tick", payload))
    manager.set_l2_quote_callback(lambda payload: captured.setdefault("l2quote", payload))
    manager.set_l2_transaction_callback(lambda payload: captured.setdefault("l2transaction", payload))
    manager.set_l2_order_callback(lambda payload: captured.setdefault("l2order", payload))
    manager.set_l2_orderqueue_callback(lambda payload: captured.setdefault("l2orderqueue", payload))

    manager.push_mock_tick("000001", 10.0)
    manager.push_mock_l2_quote("000001", 10.0, limit_up_price=11.0)
    manager.push_mock_l2_transaction("000001", 10.0, 5000, side="BUY")
    manager.push_mock_l2_order("000001", 11.0, 10000, side="BUY", entrust_no="E1")
    manager.push_mock_l2_orderqueue("000001", 11.0, [100, 200, 300])

    assert captured["tick"]["000001"].stock_code == "000001"
    assert isinstance(captured["l2quote"]["000001"], L2QuoteEvent)
    assert isinstance(captured["l2transaction"]["000001"][0], L2TransactionEvent)
    assert isinstance(captured["l2order"]["000001"][0], L2OrderEvent)
    assert isinstance(captured["l2orderqueue"]["000001"], L2OrderQueueEvent)
    assert manager.get_latest_data_status()["last_recv_time"] is not None


def test_data_subscription_can_disable_latest_status_console_print(monkeypatch):
    calls = []
    monkeypatch.setattr(DataSubscriptionManager, "_print_latest_data_status", lambda *args: calls.append(args))

    manager = DataSubscriptionManager(print_latest_status=False)
    manager.push_mock_tick("000001", 10.0)

    assert calls == []


def test_l2_subscription_diagnostics_record_sub_ids(monkeypatch):
    class FakeXtData:
        def __init__(self):
            self.calls = []

        def connect(self):
            return object()

        def get_data_dir(self):
            return "fake-data-dir"

        def subscribe_quote(self, xt_code, period, count, callback):
            self.calls.append((xt_code, period, count, callback))
            if period == "l2transaction":
                return None
            return 123

    fake_xtdata = FakeXtData()
    monkeypatch.setattr(data_subscription_module, "_XT_AVAILABLE", True)
    monkeypatch.setattr(data_subscription_module, "xtdata", fake_xtdata)

    manager = DataSubscriptionManager()
    manager.subscribe_l2_stocks(["000001"], kinds=["l2order", "l2transaction"])

    diagnostics = {
        item["kind"]: item
        for item in manager.get_l2_subscription_diagnostics()
        if item["stock"] == "000001"
    }
    assert diagnostics["l2order"]["xt_code"] == "000001.SZ"
    assert diagnostics["l2order"]["sub_id"] == 123
    assert diagnostics["l2order"]["status"] == "SUBSCRIBED"
    assert diagnostics["l2transaction"]["sub_id"] == -1
    assert diagnostics["l2transaction"]["status"] == "INVALID_SUB_ID"


def test_connection_manager_exposes_startup_diagnostics_without_secrets():
    conn = ConnectionManager(
        qmt_path=r"C:\QMT\userdata_mini",
        account_id="123456",
        account_type="STOCK",
    )

    info = conn.get_startup_config()

    assert info["qmt_path"] == r"C:\QMT\userdata_mini"
    assert info["account_id"] == "123456"
    assert info["account_type"] == "STOCK"
    assert "session_id" in info
    assert "password" not in info


def test_connection_manager_trading_ready_requires_account_subscription_success():
    class _ConnectedTrader:
        def is_connected(self):
            return True

    conn = ConnectionManager(
        qmt_path=r"C:\QMT\userdata_mini",
        account_id="123456",
        account_type="STOCK",
    )
    conn._trader = _ConnectedTrader()
    conn._account = object()
    conn._connected = True
    conn._last_error = {}

    assert conn.is_trading_ready() is True

    conn._last_error = {"stage": "account_subscribe", "return_code": -1}
    assert conn.is_trading_ready() is False

    conn._last_error = {}
    conn._connected = False
    assert conn.is_trading_ready() is False


def test_data_subscription_parses_l2_order_direction_and_cancel():
    buy = DataSubscriptionManager._parse_l2_order_record(
        "001259",
        {
            "time": 1779342217070,
            "price": 88.58,
            "volume": 800,
            "entrustNo": 43360644,
            "entrustType": 1,
            "entrustDirection": 1,
        },
        datetime.now(),
    )
    cancel_buy = DataSubscriptionManager._parse_l2_order_record(
        "600604",
        {
            "time": 1779342217070,
            "price": 5.94,
            "volume": 100,
            "entrustNo": 12480951,
            "entrustType": 1,
            "entrustDirection": 3,
        },
        datetime.now(),
    )

    assert buy.side == "BUY"
    assert buy.entrust_type == 1
    assert buy.entrust_direction == 1
    assert buy.is_cancel is False
    assert cancel_buy.side == "CANCEL_BUY"
    assert cancel_buy.is_cancel is True


def test_data_subscription_parses_l2_transaction_cancel_fields():
    event = DataSubscriptionManager._parse_l2_transaction_record(
        "001259",
        {
            "time": 1779341718330,
            "price": 0.0,
            "volume": 600,
            "amount": 0.0,
            "tradeIndex": 42120464,
            "buyNo": 41505515,
            "sellNo": 0,
            "tradeType": 0,
            "tradeFlag": 3,
        },
        datetime.now(),
    )

    assert event.trade_index == "42120464"
    assert event.buy_no == "41505515"
    assert event.sell_no == "0"
    assert event.trade_type == 0
    assert event.trade_flag == 3
    assert event.price == 0.0


def test_data_subscription_parses_l2_orderqueue_partial_coverage():
    event = DataSubscriptionManager._parse_l2_orderqueue(
        "001259",
        {
            "time": 1779342231000,
            "bidLevelPrice": 88.58,
            "bidLevelVolume": [1, 2, 2, 5],
            "bidLevelNumber": 704,
        },
        datetime.now(),
    )

    assert event.bid_level_volume == [1, 2, 2, 5]
    assert event.observed_queue_count == 4
    assert event.reported_total_order_count == 704
    assert event.is_partial_queue is True


def test_stock_selection_subprocess_entry_applies_runtime_overrides():
    configs = _select_configs_in_subprocess(
        _SettingsAwareSelectionStrategy,
        {
            "CYTRADE_MAIN_SEAL_FOLLOW_CSV_PATH": "data/stock_pools/manual/main_seal_follow_manual_pool.csv",
        },
    )

    assert len(configs) == 1
    assert configs[0].stock_code == "600162"
    assert configs[0].params["csv_path"] == "data/stock_pools/manual/main_seal_follow_manual_pool.csv"


def test_runner_syncs_l2_subscription_plan_and_dispatches_events():
    fake_data_sub = _FakeDataSubscription()
    runner = StrategyRunner(data_subscription=fake_data_sub)
    strategy = _DummyL2Strategy(StrategyConfig(stock_code="000001"))
    runner.add_strategy(strategy)

    runner._sync_subscriptions()

    assert fake_data_sub.tick_codes == {"000001"}
    assert fake_data_sub.l2_map["000001"] == {"l2quote", "l2transaction", "l2order", "l2orderqueue"}

    runner._running = True
    runner.on_l2_quote_data({"000001": L2QuoteEvent(stock_code="000001", last_price=10.0)})
    runner.on_l2_transaction_data({"000001": [L2TransactionEvent(stock_code="000001", price=10.0, volume=1000)]})
    runner.on_l2_order_data({"000001": [L2OrderEvent(stock_code="000001", price=11.0, volume=2000)]})
    runner.on_l2_orderqueue_data({"000001": L2OrderQueueEvent(stock_code="000001", price=11.0, bid_level_volume=[100])})

    assert len(strategy.l2_quote_events) == 1
    assert len(strategy.l2_transaction_events) == 1
    assert len(strategy.l2_order_events) == 1
    assert len(strategy.l2_orderqueue_events) == 1
    runtime_status = runner.get_runtime_status()
    assert runtime_status["strategy_count"] == 1
    assert runtime_status["last_strategy_event"].startswith("l2orderqueue:")
    assert runtime_status["last_strategy_event_time"] is not None


def test_runner_expands_dynamic_l2_subscription_after_quote():
    fake_data_sub = _FakeDataSubscription()
    runner = StrategyRunner(data_subscription=fake_data_sub)
    strategy = _DummyDynamicL2Strategy(StrategyConfig(stock_code="000001"))
    runner.add_strategy(strategy)

    runner._sync_subscriptions()

    assert fake_data_sub.tick_codes == set()
    assert fake_data_sub.l2_map["000001"] == {"l2quote"}

    runner._running = True
    runner.on_l2_quote_data({"000001": L2QuoteEvent(stock_code="000001", last_price=10.0)})

    assert fake_data_sub.l2_map["000001"] == {
        "l2quote",
        "l2transaction",
        "l2order",
        "l2orderqueue",
    }

    strategy.detail_enabled = False
    runner._sync_subscriptions()

    assert fake_data_sub.l2_map["000001"] == {"l2quote"}


def test_runner_keeps_tick_only_subscription_and_dispatch_flow():
    fake_data_sub = _FakeDataSubscription()
    runner = StrategyRunner(data_subscription=fake_data_sub)
    strategy = _DummyTickStrategy(StrategyConfig(stock_code="000001"))
    runner.add_strategy(strategy)

    runner._sync_subscriptions()

    assert fake_data_sub.tick_codes == {"000001"}
    assert fake_data_sub.l2_map == {}

    runner._running = True
    runner.on_market_data({"000001": TickData(stock_code="000001", last_price=10.0)})

    assert len(strategy.ticks) == 1
    assert strategy.ticks[0].stock_code == "000001"
