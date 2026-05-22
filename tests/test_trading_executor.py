from config.enums import OrderStatus
from main import _validate_live_trading_preflight
from trading.executor import TradeExecutor
from trading.order_manager import OrderManager


class _FakeSettings:
    def __init__(self, dry_run=True):
        self.CYTRADE_MAIN_SEAL_FOLLOW_DRY_RUN = dry_run


class _FakeAccount:
    account_id = "test-account"
    account_type = "STOCK"


class _FakeAsset:
    def __init__(self, cash, total_asset=0):
        self.cash = cash
        self.total_asset = total_asset


class _FakeTrader:
    def __init__(self):
        self.orders = []
        self.cancels = []

    def is_connected(self):
        return True

    def order_stock_async(self, *args):
        self.orders.append(args)
        return 12345

    def cancel_order_stock(self, account, xt_order_id):
        self.cancels.append((account, xt_order_id))


class _FakeConnection:
    def __init__(self, trader=None, account=None, ready=False, last_error=None, asset=None):
        self._trader = trader
        self._account = account
        self._ready = ready
        self._last_error = last_error or {}
        self._asset = asset

    def get_trader(self):
        return self._trader

    @property
    def account(self):
        return self._account

    def is_trading_ready(self):
        return self._ready

    def get_last_error(self):
        return dict(self._last_error)

    def query_stock_asset(self):
        return self._asset


class _FakePreflightExecutor:
    def __init__(self, status, snapshot=None):
        self._status = status
        self._snapshot = snapshot or {"asset_available": True, "available_cash": 10000.0, "total_asset": 10000.0}

    def get_live_guard_status(self):
        return dict(self._status)

    def get_live_account_snapshot(self):
        return dict(self._snapshot)


def test_trade_executor_dry_run_keeps_mock_order_path(monkeypatch):
    monkeypatch.setattr("trading.executor._XT_AVAILABLE", False)
    order_mgr = OrderManager()
    executor = TradeExecutor(None, order_mgr, live_trading_enabled=False)

    order = executor.buy_limit("s1", "strategy", "001259", 10.01, 100)

    assert order.status == OrderStatus.WAIT_REPORTING
    assert order.xt_order_id > 0
    assert order_mgr.get_order(order.order_uuid) is order
    assert order in order_mgr.get_active_orders()


def test_trade_executor_live_without_xtquant_is_junk_not_mock(monkeypatch):
    monkeypatch.setattr("trading.executor._XT_AVAILABLE", False)
    order_mgr = OrderManager()
    executor = TradeExecutor(None, order_mgr, live_trading_enabled=True)

    order = executor.buy_limit("s1", "strategy", "001259", 10.01, 100)

    assert order.status == OrderStatus.JUNK
    assert order.xt_order_id == 0
    assert "live_trading_not_ready:xtquant_unavailable" in order.status_msg
    assert order_mgr.get_order(order.order_uuid) is order
    assert order not in order_mgr.get_active_orders()


def test_trade_executor_live_ready_submits_async_order(monkeypatch):
    monkeypatch.setattr("trading.executor._XT_AVAILABLE", True)
    trader = _FakeTrader()
    account = _FakeAccount()
    conn = _FakeConnection(trader=trader, account=account, ready=True, asset=_FakeAsset(cash=5000))
    order_mgr = OrderManager()
    executor = TradeExecutor(conn, order_mgr, live_trading_enabled=True)

    order = executor.buy_limit("s1", "strategy", "001259", 10.01, 100, remark="live")

    assert order.status == OrderStatus.WAIT_REPORTING
    assert order_mgr.get_order(order.order_uuid) is order
    assert len(trader.orders) == 1
    args = trader.orders[0]
    assert args[0] is account
    assert args[1] == "001259.SZ"
    assert args[3] == 100
    assert args[5] == 10.01
    assert order_mgr._seq_to_uuid[12345] == order.order_uuid
    assert order.xt_fields["preflight_available_cash"] == 5000.0
    assert order.xt_fields["preflight_required_amount"] == 1001.0


def test_trade_executor_live_blocks_insufficient_cash(monkeypatch):
    monkeypatch.setattr("trading.executor._XT_AVAILABLE", True)
    trader = _FakeTrader()
    conn = _FakeConnection(
        trader=trader,
        account=_FakeAccount(),
        ready=True,
        asset=_FakeAsset(cash=500),
    )
    order_mgr = OrderManager()
    executor = TradeExecutor(conn, order_mgr, live_trading_enabled=True)

    order = executor.buy_limit("s1", "strategy", "001259", 10.01, 100)

    assert order.status == OrderStatus.JUNK
    assert "insufficient_cash:available_cash=500.00:required_amount=1001.00" in order.status_msg
    assert trader.orders == []


def test_trade_executor_live_blocks_asset_query_failure(monkeypatch):
    monkeypatch.setattr("trading.executor._XT_AVAILABLE", True)
    trader = _FakeTrader()
    conn = _FakeConnection(trader=trader, account=_FakeAccount(), ready=True, asset=None)
    order_mgr = OrderManager()
    executor = TradeExecutor(conn, order_mgr, live_trading_enabled=True)

    order = executor.buy_limit("s1", "strategy", "001259", 10.01, 100)

    assert order.status == OrderStatus.JUNK
    assert "buying_power_asset_unavailable:required_amount=1001.00" in order.status_msg
    assert trader.orders == []


def test_trade_executor_rejects_invalid_order_params_before_live_submit(monkeypatch):
    monkeypatch.setattr("trading.executor._XT_AVAILABLE", True)
    trader = _FakeTrader()
    conn = _FakeConnection(trader=trader, account=_FakeAccount(), ready=True, asset=_FakeAsset(cash=5000))
    order_mgr = OrderManager()
    executor = TradeExecutor(conn, order_mgr, live_trading_enabled=True)

    order = executor.buy_limit("s1", "strategy", "bad-code", 10.01, 100)

    assert order.status == OrderStatus.JUNK
    assert order.status_msg == "invalid_stock_code"
    assert trader.orders == []


def test_trade_executor_live_cancel_requires_trading_ready(monkeypatch):
    monkeypatch.setattr("trading.executor._XT_AVAILABLE", True)
    trader = _FakeTrader()
    account = _FakeAccount()
    conn = _FakeConnection(
        trader=trader,
        account=account,
        ready=False,
        last_error={"stage": "account_subscribe", "return_code": -1, "account_id": "test-account"},
    )
    order_mgr = OrderManager()
    mock_executor = TradeExecutor(None, order_mgr, live_trading_enabled=False)
    order = mock_executor.buy_limit("s1", "strategy", "001259", 10.01, 100)
    executor = TradeExecutor(conn, order_mgr, live_trading_enabled=True)

    assert executor.cancel_order(order.order_uuid, remark="cancel") is False
    assert order.status == OrderStatus.WAIT_REPORTING
    assert trader.cancels == []


def test_live_trading_preflight_blocks_live_when_guard_not_ready():
    ctx = {
        "settings": _FakeSettings(dry_run=False),
        "trade_exec": _FakePreflightExecutor({
            "live_trading_enabled": True,
            "xtquant_available": True,
            "has_trader": True,
            "has_account": True,
            "trading_ready": False,
            "last_error": {"stage": "account_subscribe", "return_code": -1},
        }),
    }

    assert _validate_live_trading_preflight(ctx, mode="unit") is False


def test_live_trading_preflight_allows_live_when_guard_and_asset_ready():
    ctx = {
        "settings": _FakeSettings(dry_run=False),
        "trade_exec": _FakePreflightExecutor({
            "live_trading_enabled": True,
            "xtquant_available": True,
            "has_trader": True,
            "has_account": True,
            "trading_ready": True,
            "last_error": {},
        }),
    }

    assert _validate_live_trading_preflight(ctx, mode="unit") is True
