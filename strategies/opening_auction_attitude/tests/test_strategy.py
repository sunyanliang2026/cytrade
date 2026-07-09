from datetime import datetime

from core.l2_models import L2OrderEvent, L2OrderQueueEvent, L2QuoteEvent, L2TransactionEvent
from core.models import TickData
from strategy.models import StrategyConfig
from strategies.opening_auction_attitude import AUCTION_STRONG_CONFIRMED, OPEN_DIRECT_PULL, OpeningAuctionAttitudeStrategy


def _ts(clock: str) -> datetime:
    hour, minute, second = [int(part) for part in clock.split(":")]
    return datetime(2026, 6, 5, hour, minute, second)


def _strategy(**params) -> OpeningAuctionAttitudeStrategy:
    return OpeningAuctionAttitudeStrategy(
        StrategyConfig(
            stock_code="000001",
            params={
                "pre_close": 10.0,
                **params,
            },
        )
    )


def test_required_data_kinds_declares_tick_and_all_l2_channels():
    assert OpeningAuctionAttitudeStrategy.required_data_kinds() == {
        "tick",
        "l2quote",
        "l2transaction",
        "l2order",
        "l2orderqueue",
    }


def test_current_data_kinds_can_limit_l2_channels_for_dynamic_candidates():
    strategy = _strategy(l2_kinds=["l2order", "l2transaction"])

    assert strategy.current_data_kinds() == {"tick", "l2order", "l2transaction"}


def test_on_tick_is_observe_only_and_records_window_price_point():
    strategy = _strategy()

    result = strategy.on_tick(
        TickData(
            stock_code="000001",
            last_price=10.2,
            pre_close=10.0,
            amount=2_000_000,
            data_time=_ts("09:24:50"),
        )
    )

    assert result is None
    l1 = strategy.build_l1_window()
    assert len(l1.points) == 1
    assert l1.points[0].price == 10.2
    assert l1.points[0].matched_amount == 2_000_000


def test_window_outside_events_are_ignored():
    strategy = _strategy()

    strategy.on_l2_quote(
        L2QuoteEvent(
            stock_code="000001",
            last_price=10.2,
            pre_close=10.0,
            event_time=_ts("09:25:06"),
            raw_xt_fields={"amount": 2_000_000},
        )
    )
    strategy.on_l2_order(
        L2OrderEvent(
            stock_code="000001",
            price=10.2,
            volume=10_000,
            amount=102_000,
            side="BUY",
            event_time=_ts("09:25:06"),
        )
    )

    assert strategy.build_l1_window().points == []
    assert strategy.build_l2_window().l2order_count == 0


def test_l2_quote_order_transaction_and_orderqueue_are_aggregated():
    strategy = _strategy()

    strategy.on_l2_quote(
        L2QuoteEvent(
            stock_code="000001",
            last_price=10.0,
            pre_close=10.0,
            event_time=_ts("09:24:50"),
            raw_xt_fields={"amount": 1_000_000},
        )
    )
    strategy.on_l2_quote(
        L2QuoteEvent(
            stock_code="000001",
            last_price=10.3,
            pre_close=10.0,
            event_time=_ts("09:25:05"),
            raw_xt_fields={"amount": 3_000_000},
        )
    )
    strategy.on_l2_order(
        L2OrderEvent(
            stock_code="000001",
            price=10.3,
            volume=10_000,
            amount=1_200_000,
            side="BUY",
            event_time=_ts("09:25:00"),
        )
    )
    strategy.on_l2_order(
        L2OrderEvent(
            stock_code="000001",
            price=10.3,
            volume=10_000,
            amount=200_000,
            side="SELL",
            event_time=_ts("09:25:00"),
        )
    )
    strategy.on_l2_order(
        L2OrderEvent(
            stock_code="000001",
            price=10.2,
            volume=1_000,
            amount=50_000,
            side="CANCEL_BUY",
            event_time=_ts("09:25:00"),
        )
    )
    strategy.on_l2_transaction(
        L2TransactionEvent(
            stock_code="000001",
            price=10.3,
            volume=10_000,
            amount=800_000,
            trade_flag=1,
            event_time=_ts("09:25:00"),
        )
    )
    strategy.on_l2_transaction(
        L2TransactionEvent(
            stock_code="000001",
            price=10.3,
            volume=10_000,
            amount=100_000,
            trade_flag=2,
            event_time=_ts("09:25:00"),
        )
    )
    strategy.on_l2_orderqueue(L2OrderQueueEvent(stock_code="000001", event_time=_ts("09:25:00")))

    l2 = strategy.build_l2_window()
    assert l2.l2quote_count == 2
    assert l2.l2order_count == 3
    assert l2.l2transaction_count == 2
    assert l2.l2orderqueue_count == 1
    assert l2.big_buy_order_amount == 1_200_000
    assert l2.big_sell_order_amount == 200_000
    assert l2.cancel_buy_order_amount == 50_000
    assert l2.big_buy_trade_amount == 800_000
    assert l2.big_sell_trade_amount == 100_000


def test_auction_reference_metrics_link_final_trades_to_late_buy_orders():
    strategy = _strategy()

    strategy.on_l2_quote(
        L2QuoteEvent(
            stock_code="000001",
            last_price=10.3,
            pre_close=10.0,
            limit_up_price=11.0,
            event_time=_ts("09:25:00"),
            raw_xt_fields={"lastPrice": 10.3, "open": 10.3, "amount": 3_000_000},
        )
    )
    strategy.on_l2_order(
        L2OrderEvent(
            stock_code="000001",
            price=10.2,
            volume=20_000,
            amount=200_000,
            side="BUY",
            entrust_no="B20",
            event_time=_ts("09:24:45"),
        )
    )
    strategy.on_l2_order(
        L2OrderEvent(
            stock_code="000001",
            price=11.0,
            volume=100_000,
            amount=1_000_000,
            side="BUY",
            entrust_no="B10",
            event_time=_ts("09:24:55"),
        )
    )
    strategy.on_l2_transaction(
        L2TransactionEvent(
            stock_code="000001",
            price=10.3,
            volume=20_000,
            amount=200_000,
            buy_no="B20",
            event_time=_ts("09:25:00"),
        )
    )
    strategy.on_l2_transaction(
        L2TransactionEvent(
            stock_code="000001",
            price=10.3,
            volume=80_000,
            amount=800_000,
            buy_no="B10",
            event_time=_ts("09:25:00"),
        )
    )

    metrics = strategy.build_auction_reference_metrics()

    assert round(metrics["open_pct"], 2) == 3.0
    assert metrics["final_auction_amount"] == 3_000_000
    assert metrics["last10_bid_amount"] == 1_000_000
    assert metrics["final_from_last10_bid_pct"] == 80.0
    assert metrics["final_from_limit_up_bid_pct"] == 80.0


def test_l2_quote_uses_auction_book_matched_and_unmatched_volumes():
    strategy = _strategy()

    strategy.on_l2_quote(
        L2QuoteEvent(
            stock_code="000001",
            last_price=15.38,
            pre_close=15.0,
            event_time=_ts("09:25:00"),
            raw_xt_fields={
                "bidPrice": [15.38, 0.0, 0.0],
                "askPrice": [15.38, 0.0, 0.0],
                "bidVol": [978, 0, 0],
                "askVol": [978, 301, 0],
                "amount": 0.0,
            },
        )
    )

    point = strategy.build_l1_window().points[0]
    assert point.amount_source == "auction_book"
    assert point.matched_volume == 978
    assert point.unmatched_buy_volume == 0
    assert point.unmatched_sell_volume == 301
    assert point.matched_amount == 15.38 * 978 * 100
    assert point.unmatched_sell_amount == 15.38 * 301 * 100


def test_reference_metrics_fallback_to_latest_auction_quote_before_0925():
    strategy = _strategy()

    strategy.on_l2_quote(
        L2QuoteEvent(
            stock_code="000001",
            last_price=10.4,
            pre_close=10.0,
            event_time=_ts("09:24:59"),
            raw_xt_fields={
                "bidPrice": [10.4, 0.0, 0.0],
                "askPrice": [10.4, 0.0, 0.0],
                "bidVol": [30_000, 0, 0],
                "askVol": [30_000, 100, 0],
                "amount": 0.0,
            },
        )
    )

    metrics = strategy.build_auction_reference_metrics()

    assert metrics["open_price_0925"] == 10.4
    assert round(metrics["open_pct"], 2) == 4.0
    assert metrics["final_auction_amount"] == 10.4 * 30_000 * 100
    assert metrics["final_amount_source"] == "quote_latest_auction"


def test_classify_auction_builds_event_payload_without_trade_signal():
    strategy = _strategy()
    assert strategy.on_tick(
        TickData(stock_code="000001", last_price=10.0, pre_close=10.0, amount=1_000_000, data_time=_ts("09:24:50"))
    ) is None
    assert strategy.on_tick(
        TickData(stock_code="000001", last_price=10.3, pre_close=10.0, amount=3_000_000, data_time=_ts("09:25:05"))
    ) is None
    strategy.on_l2_order(
        L2OrderEvent(
            stock_code="000001",
            price=10.3,
            volume=10_000,
            amount=2_000_000,
            side="BUY",
            event_time=_ts("09:25:00"),
        )
    )

    decision = strategy.classify_auction()
    payload = strategy.build_event_payload(decision)

    assert decision.auction_label == AUCTION_STRONG_CONFIRMED
    assert payload["event_name"] == "MSF_AUCTION_ATTITUDE"
    assert payload["symbol"] == "000001"
    assert payload["auction_label"] == AUCTION_STRONG_CONFIRMED
    assert payload["l2order_count"] == 1
    assert payload["evidence"]["has_order_confirmation"] is True


def test_open_verify_ticks_and_transactions_are_reported_in_event_payload():
    strategy = _strategy()
    strategy.on_tick(
        TickData(stock_code="000001", last_price=10.0, pre_close=10.0, amount=1_000_000, data_time=_ts("09:24:50"))
    )
    strategy.on_tick(
        TickData(stock_code="000001", last_price=10.3, pre_close=10.0, amount=3_000_000, data_time=_ts("09:25:05"))
    )
    strategy.on_l2_order(
        L2OrderEvent(
            stock_code="000001",
            price=10.3,
            volume=10_000,
            amount=2_000_000,
            side="BUY",
            event_time=_ts("09:25:00"),
        )
    )
    strategy.on_tick(
        TickData(stock_code="000001", last_price=10.30, pre_close=10.0, amount=4_000_000, data_time=_ts("09:30:00"))
    )
    strategy.on_tick(
        TickData(stock_code="000001", last_price=10.38, pre_close=10.0, amount=7_000_000, data_time=_ts("09:30:10"))
    )
    strategy.on_l2_transaction(
        L2TransactionEvent(
            stock_code="000001",
            price=10.38,
            volume=10_000,
            amount=800_000,
            trade_flag=1,
            event_time=_ts("09:30:10"),
        )
    )
    strategy.on_l2_transaction(
        L2TransactionEvent(
            stock_code="000001",
            price=10.36,
            volume=10_000,
            amount=100_000,
            trade_flag=2,
            event_time=_ts("09:30:20"),
        )
    )

    decision = strategy.classify_auction()
    payload = strategy.build_event_payload(decision)

    assert payload["auction_label"] == AUCTION_STRONG_CONFIRMED
    assert payload["open_verify_path"] == OPEN_DIRECT_PULL
    assert payload["open_point_count"] == 2
    assert payload["open_l2transaction_count"] == 2
    assert payload["open_buy_trade_amount"] == 800_000
    assert payload["open_sell_trade_amount"] == 100_000
    assert payload["open_evidence"]["has_buy_confirmation"] is True
