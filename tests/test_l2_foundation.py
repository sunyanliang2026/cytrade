from core.data_subscription import DataSubscriptionManager
from core.l2_models import L2OrderEvent, L2OrderQueueEvent, L2QuoteEvent, L2TransactionEvent
from core.models import TickData
from strategy.base import BaseStrategy
from strategy.models import StrategyConfig
from strategy.runner import StrategyRunner


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
