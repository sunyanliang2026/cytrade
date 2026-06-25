"""Pure scoring functions for the opening-auction attitude strategy."""
from __future__ import annotations

from dataclasses import asdict

from strategies.opening_auction_attitude.models import (
    AUCTION_BIG_ORDER_CONFIRMED,
    AUCTION_BIG_TRADE_CONFIRMED,
    AUCTION_FAKE_RISK,
    AUCTION_MONEY_LIFT,
    AUCTION_NO_SIGNAL,
    AUCTION_SPEED_ONLY,
    AUCTION_STRONG_CONFIRMED,
    OPEN_DIRECT_PULL,
    OPEN_FAKE_BREAKDOWN,
    OPEN_NO_FOLLOW_THROUGH,
    OPEN_WASH_THEN_PULL,
    AuctionDecision,
    AuctionL1Window,
    AuctionL2Window,
    AuctionPricePoint,
    AuctionScoreConfig,
    AuctionWindowMetrics,
    OpenVerifyConfig,
    OpenVerifyDecision,
    OpenVerifyMetrics,
    OpenVerifyPoint,
    OpenVerifyWindow,
)


def calculate_auction_window_metrics(
    l1: AuctionL1Window,
    l2: AuctionL2Window | None = None,
    config: AuctionScoreConfig | None = None,
) -> AuctionWindowMetrics:
    """Calculate low-to-final price and money-lift metrics."""

    cfg = config or AuctionScoreConfig()
    l2 = l2 or AuctionL2Window(symbol=l1.symbol)
    points = _valid_points_in_window(l1.points, cfg)
    metrics = AuctionWindowMetrics(symbol=l1.symbol)
    if not points or float(l1.pre_close or 0.0) <= 0:
        _fill_l2_metrics(metrics, l2, cfg)
        return metrics

    final_point = points[-1]
    high_point = max(points, key=lambda item: item.price)
    min_price = min(point.price for point in points)
    low_point = [point for point in points if point.price == min_price][-1]

    pre_close = float(l1.pre_close)
    amount_delta = float(final_point.matched_amount or 0.0) - float(low_point.matched_amount or 0.0)
    final_amount = float(final_point.matched_amount or 0.0)

    metrics.auction_low_price = float(low_point.price or 0.0)
    metrics.auction_low_time = low_point.event_time
    metrics.auction_final_price = float(final_point.price or 0.0)
    metrics.auction_final_time = final_point.event_time
    metrics.auction_high_price = float(high_point.price or 0.0)
    metrics.auction_high_time = high_point.event_time
    metrics.final_gap_pct = (metrics.auction_final_price - pre_close) / pre_close
    metrics.low_to_final_lift_pct = (metrics.auction_final_price - metrics.auction_low_price) / pre_close
    metrics.low_to_final_amount_delta = amount_delta
    metrics.amount_at_low = float(low_point.matched_amount or 0.0)
    metrics.amount_at_final = final_amount
    metrics.amount_source_at_low = str(low_point.amount_source or "")
    metrics.amount_source_at_final = str(final_point.amount_source or "")
    metrics.amount_is_cumulative = _is_cumulative_amount_source(metrics.amount_source_at_final)
    if metrics.amount_is_cumulative:
        metrics.low_to_final_amount_ratio = max(0.0, amount_delta) / final_amount if final_amount > 0 else 0.0
    else:
        metrics.low_to_final_amount_ratio = 0.0
    metrics.matched_volume_at_low = float(low_point.matched_volume or 0.0)
    metrics.matched_volume_at_final = float(final_point.matched_volume or 0.0)
    metrics.unmatched_buy_volume_at_final = float(final_point.unmatched_buy_volume or 0.0)
    metrics.unmatched_sell_volume_at_final = float(final_point.unmatched_sell_volume or 0.0)
    metrics.unmatched_buy_amount_at_final = float(final_point.unmatched_buy_amount or 0.0)
    metrics.unmatched_sell_amount_at_final = float(final_point.unmatched_sell_amount or 0.0)
    metrics.unmatched_amount_imbalance_at_final = (
        metrics.unmatched_buy_amount_at_final - metrics.unmatched_sell_amount_at_final
    )
    unmatched_total = metrics.unmatched_buy_amount_at_final + metrics.unmatched_sell_amount_at_final
    metrics.has_unmatched_sell_pressure = (
        unmatched_total > 0
        and metrics.unmatched_sell_amount_at_final / unmatched_total >= cfg.unmatched_sell_pressure_ratio_threshold
        and metrics.unmatched_sell_amount_at_final > metrics.unmatched_buy_amount_at_final
    )
    metrics.final_near_high = (metrics.auction_high_price - metrics.auction_final_price) / pre_close <= cfg.close_to_high_tolerance_pct
    _fill_l2_metrics(metrics, l2, cfg)
    return metrics


def evaluate_auction_attitude(
    l1: AuctionL1Window,
    l2: AuctionL2Window | None = None,
    config: AuctionScoreConfig | None = None,
) -> AuctionDecision:
    """Return the auction attitude label, scores, and explainable evidence."""

    cfg = config or AuctionScoreConfig()
    l2 = l2 or AuctionL2Window(symbol=l1.symbol)
    metrics = calculate_auction_window_metrics(l1, l2, cfg)
    speed_score = _score_speed(metrics, cfg)
    attitude_score = _score_attitude(metrics, cfg)

    has_lift = metrics.low_to_final_lift_pct >= cfg.min_low_to_final_lift_pct
    has_gap = metrics.final_gap_pct >= cfg.min_final_gap_pct
    has_money_lift = _has_money_evidence(metrics, cfg)
    has_strong_money_lift = _has_strong_money_evidence(metrics, cfg)

    label = AUCTION_NO_SIGNAL
    reason = "no_effective_low_to_final_lift"

    if not has_lift:
        reason = "low_to_final_lift_below_threshold"
    elif not has_gap:
        reason = "final_gap_below_threshold"
    elif metrics.amount_is_cumulative and metrics.low_to_final_amount_delta <= 0:
        label = AUCTION_FAKE_RISK
        reason = "price_lift_without_amount_delta"
    elif not metrics.amount_is_cumulative and metrics.amount_at_final <= 0:
        label = AUCTION_FAKE_RISK
        reason = "price_lift_without_current_matched_amount"
    elif metrics.has_order_sell_pressure:
        label = AUCTION_FAKE_RISK
        reason = "l2_order_sell_pressure"
    elif metrics.has_trade_sell_pressure:
        label = AUCTION_FAKE_RISK
        reason = "l2_trade_sell_pressure"
    elif metrics.has_unmatched_sell_pressure and not metrics.has_order_confirmation:
        label = AUCTION_FAKE_RISK
        reason = "unmatched_sell_pressure_without_order_confirmation"
    elif not metrics.final_near_high:
        label = AUCTION_FAKE_RISK
        reason = "final_price_not_near_window_high"
    elif not has_money_lift:
        label = AUCTION_SPEED_ONLY
        reason = "price_lift_but_money_ratio_insufficient"
    elif metrics.has_order_confirmation and (
        not metrics.has_trade_data or metrics.has_trade_confirmation or metrics.big_trade_imbalance >= 0
    ):
        if has_strong_money_lift:
            label = AUCTION_STRONG_CONFIRMED
            reason = "money_lift_with_big_order_confirmation"
        else:
            label = AUCTION_BIG_ORDER_CONFIRMED
            reason = "big_order_confirmed_money_lift"
    elif metrics.has_trade_confirmation:
        label = AUCTION_BIG_TRADE_CONFIRMED
        reason = "big_trade_confirmed_money_lift"
    elif has_money_lift:
        label = AUCTION_MONEY_LIFT
        reason = "money_lift_without_l2_confirmation"

    return AuctionDecision(
        symbol=l1.symbol,
        auction_label=label,
        auction_speed_score=round(speed_score, 3),
        auction_attitude_score=round(attitude_score, 3),
        reason=reason,
        evidence=asdict(metrics),
    )


def calculate_open_verify_metrics(
    window: OpenVerifyWindow,
    config: OpenVerifyConfig | None = None,
) -> OpenVerifyMetrics:
    """Calculate 09:30-09:35 price-path and buy/sell confirmation metrics."""

    cfg = config or OpenVerifyConfig()
    points = _valid_open_points(window.points, cfg)
    metrics = OpenVerifyMetrics(symbol=window.symbol)
    metrics.point_count = len(points)

    trade_buy = float(window.buy_trade_amount or 0.0)
    trade_sell = float(window.sell_trade_amount or 0.0)
    trade_total = trade_buy + trade_sell
    metrics.has_trade_data = trade_total > 0
    metrics.buy_trade_ratio = trade_buy / trade_total if trade_total > 0 else 0.0
    metrics.sell_trade_ratio = trade_sell / trade_total if trade_total > 0 else 0.0
    metrics.has_buy_confirmation = not metrics.has_trade_data or metrics.buy_trade_ratio >= cfg.min_buy_trade_ratio
    metrics.has_sell_pressure = (
        metrics.has_trade_data
        and metrics.sell_trade_ratio >= cfg.sell_pressure_ratio_threshold
        and trade_sell > trade_buy
    )

    if not points:
        return metrics

    open_point = points[0]
    open_price = float(open_point.price or 0.0)
    if open_price <= 0:
        return metrics

    high_point = max(points, key=lambda item: item.price)
    low_point = min(points, key=lambda item: item.price)
    final_point = points[-1]

    metrics.open_price = open_price
    metrics.open_time = open_point.event_time
    metrics.high_price = float(high_point.price or 0.0)
    metrics.high_time = high_point.event_time
    metrics.low_price = float(low_point.price or 0.0)
    metrics.low_time = low_point.event_time
    metrics.final_price = float(final_point.price or 0.0)
    metrics.final_time = final_point.event_time
    metrics.max_gain_from_open_pct = (metrics.high_price - open_price) / open_price
    metrics.max_drawdown_from_open_pct = (open_price - metrics.low_price) / open_price
    metrics.final_return_pct = (metrics.final_price - open_price) / open_price
    metrics.seconds_to_high = _seconds_between(open_point.event_time, high_point.event_time)

    direct_points = [
        point
        for point in points
        if _point_elapsed_seconds(open_point, point) <= cfg.direct_check_end_sec
    ]
    direct_scored_points = [
        point
        for point in direct_points
        if _point_elapsed_seconds(open_point, point) >= cfg.direct_check_start_sec
    ] or direct_points
    if direct_scored_points:
        direct_high_point = max(direct_scored_points, key=lambda item: item.price)
        metrics.direct_high_price = float(direct_high_point.price or 0.0)
        metrics.direct_high_time = direct_high_point.event_time
        metrics.direct_high_gain_pct = (metrics.direct_high_price - open_price) / open_price
    if direct_points:
        direct_low_price = min(float(point.price or 0.0) for point in direct_points)
        metrics.direct_low_drawdown_pct = (open_price - direct_low_price) / open_price

    low_index = points.index(low_point)
    before_low = points[: low_index + 1]
    metrics.first_rebound_high_price = max(float(point.price or 0.0) for point in before_low) if before_low else 0.0
    recover_level = open_price * (1 - cfg.recover_open_tolerance_pct)
    rebreak_level = max(open_price, metrics.first_rebound_high_price) * (1 + cfg.min_rebreak_pct)

    recovered_index = -1
    for index, point in enumerate(points[low_index + 1 :], start=low_index + 1):
        if float(point.price or 0.0) >= recover_level:
            recovered_index = index
            break
    metrics.recovered_open = recovered_index >= 0
    if metrics.recovered_open:
        metrics.rebreak_after_recover = any(
            float(point.price or 0.0) >= rebreak_level
            for point in points[recovered_index:]
        )

    return metrics


def evaluate_open_behavior(
    window: OpenVerifyWindow,
    config: OpenVerifyConfig | None = None,
) -> OpenVerifyDecision:
    """Return the post-open true/false auction verification path."""

    cfg = config or OpenVerifyConfig()
    metrics = calculate_open_verify_metrics(window, cfg)

    path = OPEN_NO_FOLLOW_THROUGH
    score = 0.0
    reason = "insufficient_open_points"

    if str(window.auction_label or "") == AUCTION_NO_SIGNAL:
        reason = "auction_label_not_actionable"
    elif metrics.point_count >= 2 and metrics.open_price > 0:
        direct_pull = (
            metrics.direct_high_gain_pct >= cfg.min_direct_pull_pct
            and metrics.direct_low_drawdown_pct <= cfg.max_direct_drawdown_pct
            and metrics.has_buy_confirmation
            and not metrics.has_sell_pressure
        )
        wash_then_pull = (
            metrics.max_drawdown_from_open_pct >= cfg.min_wash_dip_pct
            and metrics.max_drawdown_from_open_pct <= cfg.max_wash_drawdown_pct
            and metrics.recovered_open
            and metrics.rebreak_after_recover
            and metrics.has_buy_confirmation
            and not metrics.has_sell_pressure
        )
        fake_breakdown = (
            metrics.max_drawdown_from_open_pct >= cfg.breakdown_pct
            and (not metrics.recovered_open or metrics.final_return_pct <= -cfg.failed_recover_tolerance_pct)
            and (metrics.has_sell_pressure or not metrics.has_trade_data or metrics.final_return_pct < 0)
        )

        if direct_pull:
            path = OPEN_DIRECT_PULL
            score = 85.0
            reason = "direct_pull_confirmed"
        elif wash_then_pull:
            path = OPEN_WASH_THEN_PULL
            score = 78.0
            reason = "controlled_dip_recovered_and_rebroke"
        elif fake_breakdown:
            path = OPEN_FAKE_BREAKDOWN
            score = 10.0
            reason = "breakdown_without_recovery"
        else:
            path = OPEN_NO_FOLLOW_THROUGH
            score = 35.0
            reason = "no_open_continuation"

    return OpenVerifyDecision(
        symbol=window.symbol,
        open_verify_path=path,
        open_verify_score=round(score, 3),
        reason=reason,
        evidence=asdict(metrics),
    )


def _valid_points_in_window(points: list[AuctionPricePoint], config: AuctionScoreConfig) -> list[AuctionPricePoint]:
    indexed = []
    for index, point in enumerate(points or []):
        if float(point.price or 0.0) <= 0:
            continue
        if point.event_time is not None:
            clock = point.event_time.time()
            if clock < config.window_start or clock > config.window_end:
                continue
        indexed.append((index, point))
    indexed.sort(key=lambda item: (item[1].event_time is None, item[1].event_time or item[0], item[0]))
    return [point for _, point in indexed]


def _valid_open_points(points: list[OpenVerifyPoint], config: OpenVerifyConfig) -> list[OpenVerifyPoint]:
    indexed = []
    for index, point in enumerate(points or []):
        if float(point.price or 0.0) <= 0:
            continue
        if point.event_time is not None:
            clock = point.event_time.time()
            if clock < config.window_start or clock > config.window_end:
                continue
        indexed.append((index, point))
    indexed.sort(key=lambda item: (item[1].event_time is None, item[1].event_time or item[0], item[0]))
    return [point for _, point in indexed]


def _seconds_between(start, end) -> float:
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def _point_elapsed_seconds(open_point: OpenVerifyPoint, point: OpenVerifyPoint) -> float:
    return _seconds_between(open_point.event_time, point.event_time)


def _fill_l2_metrics(metrics: AuctionWindowMetrics, l2: AuctionL2Window, config: AuctionScoreConfig) -> None:
    order_buy = float(l2.big_buy_order_amount or 0.0)
    order_sell = float(l2.big_sell_order_amount or 0.0)
    trade_buy = float(l2.big_buy_trade_amount or 0.0)
    trade_sell = float(l2.big_sell_trade_amount or 0.0)
    order_total = order_buy + order_sell
    trade_total = trade_buy + trade_sell

    metrics.big_order_imbalance = order_buy - order_sell
    metrics.big_trade_imbalance = trade_buy - trade_sell
    metrics.big_order_buy_ratio = order_buy / order_total if order_total > 0 else 0.0
    metrics.big_trade_buy_ratio = trade_buy / trade_total if trade_total > 0 else 0.0
    metrics.has_order_confirmation = (
        int(l2.l2order_count or 0) > 0
        and order_total > 0
        and metrics.big_order_buy_ratio >= config.big_buy_ratio_threshold
        and metrics.big_order_imbalance > 0
    )
    metrics.has_trade_data = int(l2.l2transaction_count or 0) > 0
    metrics.has_trade_confirmation = (
        metrics.has_trade_data
        and trade_total > 0
        and metrics.big_trade_buy_ratio >= config.big_buy_ratio_threshold
        and metrics.big_trade_imbalance > 0
    )
    metrics.has_order_sell_pressure = (
        int(l2.l2order_count or 0) > 0
        and order_total > 0
        and order_sell / order_total >= config.sell_pressure_ratio_threshold
        and order_sell > order_buy
    )
    metrics.has_trade_sell_pressure = (
        metrics.has_trade_data
        and trade_total > 0
        and trade_sell / trade_total >= config.sell_pressure_ratio_threshold
        and trade_sell > trade_buy
    )


def _score_speed(metrics: AuctionWindowMetrics, config: AuctionScoreConfig) -> float:
    score = 0.0
    if metrics.low_to_final_lift_pct >= config.min_low_to_final_lift_pct:
        score += 35
    if metrics.low_to_final_amount_delta > 0 or (not metrics.amount_is_cumulative and metrics.amount_at_final > 0):
        score += 25
    if metrics.final_gap_pct >= config.min_final_gap_pct:
        score += 15
    if metrics.final_near_high:
        score += 15
    if _has_money_evidence(metrics, config):
        score += 10
    return max(0.0, min(100.0, score))


def _score_attitude(metrics: AuctionWindowMetrics, config: AuctionScoreConfig) -> float:
    score = 0.0
    if metrics.low_to_final_lift_pct >= config.min_low_to_final_lift_pct:
        score += 20
    if _has_money_evidence(metrics, config):
        score += 25
    if metrics.has_order_confirmation:
        score += 25
    if metrics.has_trade_confirmation:
        score += 20
    if metrics.final_near_high and not metrics.has_order_sell_pressure and not metrics.has_trade_sell_pressure:
        score += 10
    if metrics.unmatched_amount_imbalance_at_final > 0:
        score += 5
    if metrics.amount_is_cumulative and metrics.low_to_final_amount_delta <= 0:
        score -= 25
    if metrics.has_order_sell_pressure:
        score -= 25
    if metrics.has_trade_sell_pressure:
        score -= 25
    if not metrics.final_near_high:
        score -= 15
    if metrics.has_unmatched_sell_pressure:
        score -= 10
    return max(0.0, min(100.0, score))


def _is_cumulative_amount_source(source: str) -> bool:
    source = str(source or "").strip()
    return source != "non_cumulative"


def _has_money_evidence(metrics: AuctionWindowMetrics, config: AuctionScoreConfig) -> bool:
    if metrics.amount_is_cumulative:
        return (
            metrics.low_to_final_amount_delta > 0
            and metrics.low_to_final_amount_ratio >= config.min_money_lift_ratio
        )
    return metrics.amount_at_final > 0


def _has_strong_money_evidence(metrics: AuctionWindowMetrics, config: AuctionScoreConfig) -> bool:
    if metrics.amount_is_cumulative:
        return metrics.low_to_final_amount_ratio >= config.strong_money_lift_ratio
    return metrics.amount_at_final > 0


__all__ = [
    "calculate_auction_window_metrics",
    "evaluate_auction_attitude",
    "calculate_open_verify_metrics",
    "evaluate_open_behavior",
]
