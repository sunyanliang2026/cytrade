from datetime import datetime

from strategy.opening_auction_attitude import (
    AUCTION_BIG_ORDER_CONFIRMED,
    AUCTION_FAKE_RISK,
    AUCTION_MONEY_LIFT,
    AUCTION_NO_SIGNAL,
    AUCTION_SPEED_ONLY,
    AUCTION_STRONG_CONFIRMED,
    OPEN_DIRECT_PULL,
    OPEN_FAKE_BREAKDOWN,
    OPEN_NO_FOLLOW_THROUGH,
    OPEN_WASH_THEN_PULL,
    AuctionL1Window,
    AuctionL2Window,
    AuctionPricePoint,
    AuctionScoreConfig,
    OpenVerifyPoint,
    OpenVerifyWindow,
    evaluate_auction_attitude,
    evaluate_open_behavior,
)


def _ts(clock: str) -> datetime:
    hour, minute, second = [int(part) for part in clock.split(":")]
    return datetime(2026, 6, 5, hour, minute, second)


def _l1(points):
    return AuctionL1Window(
        symbol="000001",
        pre_close=10.0,
        points=[AuctionPricePoint(_ts(clock), price, amount) for clock, price, amount in points],
    )


def _open(points, *, buy=0.0, sell=0.0):
    return OpenVerifyWindow(
        symbol="000001",
        auction_label=AUCTION_STRONG_CONFIRMED,
        auction_final_price=10.0,
        points=[OpenVerifyPoint(_ts(clock), price, amount) for clock, price, amount in points],
        buy_trade_amount=buy,
        sell_trade_amount=sell,
    )


def test_no_signal_when_price_does_not_lift_from_low_to_final():
    decision = evaluate_auction_attitude(
        _l1(
            [
                ("09:24:30", 10.20, 1_000_000),
                ("09:24:55", 10.10, 1_300_000),
                ("09:25:05", 10.10, 1_500_000),
            ]
        )
    )

    assert decision.auction_label == AUCTION_NO_SIGNAL
    assert decision.reason == "low_to_final_lift_below_threshold"


def test_money_lift_when_low_to_final_amount_ratio_is_high():
    decision = evaluate_auction_attitude(
        _l1(
            [
                ("09:24:30", 10.20, 1_000_000),
                ("09:24:50", 10.00, 1_200_000),
                ("09:25:05", 10.25, 3_000_000),
            ]
        )
    )

    assert decision.auction_label == AUCTION_MONEY_LIFT
    assert decision.evidence["low_to_final_amount_ratio"] == 0.6
    assert decision.evidence["auction_low_price"] == 10.0


def test_speed_only_when_price_lifts_but_money_ratio_is_insufficient():
    decision = evaluate_auction_attitude(
        _l1(
            [
                ("09:24:30", 10.20, 2_800_000),
                ("09:24:50", 10.00, 2_900_000),
                ("09:25:05", 10.25, 3_000_000),
            ]
        )
    )

    assert decision.auction_label == AUCTION_SPEED_ONLY
    assert decision.reason == "price_lift_but_money_ratio_insufficient"


def test_big_order_confirmed_without_transaction_data():
    decision = evaluate_auction_attitude(
        _l1(
            [
                ("09:24:30", 10.18, 1_000_000),
                ("09:24:50", 10.00, 1_100_000),
                ("09:25:05", 10.24, 2_000_000),
            ]
        ),
        AuctionL2Window(
            symbol="000001",
            l2order_count=80,
            l2transaction_count=0,
            big_buy_order_amount=9_000_000,
            big_sell_order_amount=1_000_000,
        ),
        AuctionScoreConfig(strong_money_lift_ratio=0.80),
    )

    assert decision.auction_label == AUCTION_BIG_ORDER_CONFIRMED
    assert decision.evidence["has_trade_data"] is False
    assert decision.evidence["has_order_confirmation"] is True


def test_strong_confirmed_with_order_and_trade_confirmation():
    decision = evaluate_auction_attitude(
        _l1(
            [
                ("09:24:30", 10.10, 1_000_000),
                ("09:24:50", 10.00, 1_200_000),
                ("09:25:05", 10.30, 3_000_000),
            ]
        ),
        AuctionL2Window(
            symbol="000001",
            l2order_count=100,
            l2transaction_count=50,
            big_buy_order_amount=12_000_000,
            big_sell_order_amount=2_000_000,
            big_buy_trade_amount=8_000_000,
            big_sell_trade_amount=1_000_000,
        ),
    )

    assert decision.auction_label == AUCTION_STRONG_CONFIRMED
    assert decision.evidence["has_trade_confirmation"] is True
    assert decision.auction_attitude_score == 100


def test_fake_risk_when_price_lifts_without_amount_delta():
    decision = evaluate_auction_attitude(
        _l1(
            [
                ("09:24:30", 10.10, 1_000_000),
                ("09:24:50", 10.00, 1_200_000),
                ("09:25:05", 10.30, 1_200_000),
            ]
        )
    )

    assert decision.auction_label == AUCTION_FAKE_RISK
    assert decision.reason == "price_lift_without_amount_delta"


def test_trade_sell_pressure_blocks_strong_confirmation():
    decision = evaluate_auction_attitude(
        _l1(
            [
                ("09:24:30", 10.10, 1_000_000),
                ("09:24:50", 10.00, 1_200_000),
                ("09:25:05", 10.30, 3_000_000),
            ]
        ),
        AuctionL2Window(
            symbol="000001",
            l2order_count=100,
            l2transaction_count=50,
            big_buy_order_amount=12_000_000,
            big_sell_order_amount=2_000_000,
            big_buy_trade_amount=1_000_000,
            big_sell_trade_amount=8_000_000,
        ),
    )

    assert decision.auction_label == AUCTION_FAKE_RISK
    assert decision.reason == "l2_trade_sell_pressure"


def test_open_verify_direct_pull_when_price_breaks_up_quickly_with_buy_confirmation():
    decision = evaluate_open_behavior(
        _open(
            [
                ("09:30:00", 10.00, 1_000_000),
                ("09:30:10", 10.08, 3_000_000),
                ("09:30:30", 10.06, 4_000_000),
            ],
            buy=8_000_000,
            sell=2_000_000,
        )
    )

    assert decision.open_verify_path == OPEN_DIRECT_PULL
    assert decision.reason == "direct_pull_confirmed"
    assert decision.evidence["direct_high_gain_pct"] > 0.005
    assert decision.evidence["has_buy_confirmation"] is True


def test_open_verify_wash_then_pull_after_controlled_dip_recovery_and_rebreak():
    decision = evaluate_open_behavior(
        _open(
            [
                ("09:30:00", 10.00, 1_000_000),
                ("09:30:20", 9.94, 2_000_000),
                ("09:31:00", 10.00, 2_500_000),
                ("09:31:30", 10.08, 5_000_000),
            ],
            buy=7_000_000,
            sell=3_000_000,
        )
    )

    assert decision.open_verify_path == OPEN_WASH_THEN_PULL
    assert decision.reason == "controlled_dip_recovered_and_rebroke"
    assert decision.evidence["recovered_open"] is True
    assert decision.evidence["rebreak_after_recover"] is True


def test_open_verify_fake_breakdown_when_open_breaks_without_recovery():
    decision = evaluate_open_behavior(
        _open(
            [
                ("09:30:00", 10.00, 1_000_000),
                ("09:30:20", 9.88, 2_000_000),
                ("09:31:00", 9.90, 2_500_000),
                ("09:35:00", 9.89, 4_000_000),
            ],
            buy=2_000_000,
            sell=8_000_000,
        )
    )

    assert decision.open_verify_path == OPEN_FAKE_BREAKDOWN
    assert decision.reason == "breakdown_without_recovery"
    assert decision.evidence["has_sell_pressure"] is True


def test_open_verify_no_follow_through_when_price_has_no_continuation_or_breakdown():
    decision = evaluate_open_behavior(
        _open(
            [
                ("09:30:00", 10.00, 1_000_000),
                ("09:31:00", 10.02, 2_000_000),
                ("09:35:00", 10.01, 3_000_000),
            ],
            buy=5_000_000,
            sell=4_000_000,
        )
    )

    assert decision.open_verify_path == OPEN_NO_FOLLOW_THROUGH
    assert decision.reason == "no_open_continuation"


def test_open_verify_is_not_actionable_without_auction_signal():
    window = _open(
        [
            ("09:30:00", 10.00, 1_000_000),
            ("09:30:10", 10.08, 2_000_000),
        ],
        buy=8_000_000,
        sell=2_000_000,
    )
    window.auction_label = AUCTION_NO_SIGNAL

    decision = evaluate_open_behavior(window)

    assert decision.open_verify_path == OPEN_NO_FOLLOW_THROUGH
    assert decision.reason == "auction_label_not_actionable"
