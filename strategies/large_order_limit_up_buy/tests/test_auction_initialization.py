from datetime import datetime
import sys
import types

from core.l2_models import L2QuoteEvent, L2TransactionEvent
from strategy.models import StrategyConfig
from strategies.large_order_limit_up_buy.scripts import run_market_only
from strategies.large_order_limit_up_buy.strategy import LargeOrderLimitUpBuyStrategy


class RecordingLogger:
    def __init__(self):
        self.info_messages = []
        self.warning_messages = []

    def info(self, message, *args):
        self.info_messages.append(message % args if args else message)

    def warning(self, message, *args):
        self.warning_messages.append(message % args if args else message)


def make_strategy(tmp_path, code):
    return LargeOrderLimitUpBuyStrategy(
        StrategyConfig(
            stock_code=code,
            params={"stock_name": "测试", "plan_amount": 100000, "record_dir": str(tmp_path)},
        )
    )


def test_initialize_from_auction_tick_uses_bid1_only(tmp_path):
    sealed = make_strategy(tmp_path, "600001")
    unsealed = make_strategy(tmp_path, "000001")

    assert sealed.initialize_from_auction_tick(
        bid1=8.0,
        limit_up_price=8.0,
        event_time=datetime(2026, 9, 20, 9, 26),
    )
    assert sealed._entry_phase == "WAIT_REOPEN"

    assert unsealed.initialize_from_auction_tick(bid1=7.9, limit_up_price=8.0)
    assert unsealed._entry_phase == "READY"


def test_initialize_auction_states_batches_full_tick_and_falls_back_per_stock(monkeypatch, tmp_path):
    calls = []

    def fake_get_full_tick(codes):
        calls.append(list(codes))
        return {
            "600001.SH": {"bidPrice": [8.0, 7.9], "upLimitPrice": 8.0},
            "000001.SZ": {"bidPrice": [7.9], "upLimitPrice": 8.0},
        }

    monkeypatch.setitem(
        sys.modules,
        "xtquant",
        types.SimpleNamespace(xtdata=types.SimpleNamespace(get_full_tick=fake_get_full_tick)),
    )
    logger = RecordingLogger()
    strategies = [make_strategy(tmp_path, "600001"), make_strategy(tmp_path, "000001")]

    run_market_only.initialize_auction_states(
        strategies, logger, datetime(2026, 9, 20, 9, 26),
    )

    assert calls == [["600001.SH", "000001.SZ"]]
    assert [item._entry_phase for item in strategies] == ["WAIT_REOPEN", "READY"]
    assert not logger.warning_messages


def test_initialize_auction_states_reports_get_full_tick_failure_per_stock(monkeypatch, tmp_path):
    def failing_get_full_tick(codes):
        raise RuntimeError("QMT unavailable")

    monkeypatch.setitem(
        sys.modules,
        "xtquant",
        types.SimpleNamespace(xtdata=types.SimpleNamespace(get_full_tick=failing_get_full_tick)),
    )
    logger = RecordingLogger()
    strategies = [make_strategy(tmp_path, "600001"), make_strategy(tmp_path, "000001")]

    run_market_only.initialize_auction_states(
        strategies, logger, datetime(2026, 9, 20, 9, 26),
    )

    assert all(item._entry_phase == "WAIT_INITIAL_QUOTE" for item in strategies)
    assert len(logger.warning_messages) == 1
    assert "接口异常:RuntimeError" in logger.warning_messages[0]
    assert "600001 测试、000001 测试" in logger.warning_messages[0]
    assert "继续使用L2行情" in logger.warning_messages[0]


def test_initialize_auction_states_reports_invalid_full_tick_response(monkeypatch, tmp_path):
    monkeypatch.setitem(
        sys.modules,
        "xtquant",
        types.SimpleNamespace(xtdata=types.SimpleNamespace(get_full_tick=lambda codes: [])),
    )
    logger = RecordingLogger()
    strategies = [make_strategy(tmp_path, "600001")]

    run_market_only.initialize_auction_states(
        strategies, logger, datetime(2026, 9, 20, 9, 26),
    )

    assert strategies[0]._entry_phase == "WAIT_INITIAL_QUOTE"
    assert logger.warning_messages == [
        "[LARGE_ORDER] [竞价] 快照失败 1只 原因=返回格式错误：600001 测试；继续使用L2行情"
    ]


def test_initialize_auction_states_returns_only_unresolved_stocks(monkeypatch, tmp_path):
    calls = []

    def fake_get_full_tick(codes):
        calls.append(list(codes))
        return {
            "600001.SH": {"bidPrice": [8.0], "upLimitPrice": 8.0},
            "000001.SZ": {},
        }

    monkeypatch.setitem(
        sys.modules,
        "xtquant",
        types.SimpleNamespace(xtdata=types.SimpleNamespace(get_full_tick=fake_get_full_tick)),
    )
    logger = RecordingLogger()
    sealed = make_strategy(tmp_path, "600001")
    missing = make_strategy(tmp_path, "000001")
    strategies = [sealed, missing]

    failed = run_market_only.initialize_auction_states(
        strategies, logger, datetime(2026, 9, 20, 9, 25), log_failures=False,
    )
    assert failed == [missing]
    assert sealed._entry_phase == "WAIT_REOPEN"

    failed = run_market_only.initialize_auction_states(
        strategies, logger, datetime(2026, 9, 20, 9, 25, 1), log_failures=False,
    )
    assert failed == [missing]
    assert calls == [
        ["600001.SH", "000001.SZ"],
        ["600001.SH", "000001.SZ"],
    ]


def test_l2_auction_events_are_recorded_without_changing_state(tmp_path):
    strategy = make_strategy(tmp_path, "600001")
    strategy.on_l2_quote(L2QuoteEvent(
        stock_code="600001", limit_up_price=8.0, bid1=8.0,
        event_time=datetime(2026, 9, 20, 9, 25),
    ))
    strategy.on_l2_transaction(L2TransactionEvent(
        stock_code="600001", price=8.0, volume=1000,
        event_time=datetime(2026, 9, 20, 9, 25, 1),
    ))

    assert strategy._entry_phase == "WAIT_INITIAL_QUOTE"
    assert strategy._last_quote is None
    assert strategy._sealed_trade_amount == 0.0
