from argparse import Namespace

from strategy.models import StrategyConfig
from strategies.large_order_limit_up_buy.scripts.run_live import validate_live_confirmation


def args(**overrides):
    values = {
        "live": True,
        "confirm_live": True,
        "max_order_amount": 1000.0,
        "max_total_amount": 1000.0,
    }
    values.update(overrides)
    return Namespace(**values)


def config(code="600001", amount=1000.0):
    return StrategyConfig(stock_code=code, params={"plan_amount": amount})


def test_live_confirmation_accepts_csv_plan_within_limits():
    assert validate_live_confirmation(args(), [config()]) == ""


def test_live_confirmation_rejects_missing_live_ack():
    assert validate_live_confirmation(args(confirm_live=False), [config()]) == "live_requires_confirm_live"


def test_live_confirmation_rejects_limit_breach():
    assert validate_live_confirmation(args(max_order_amount=999), [config()]) == "live_plan_exceeds_max_order_amount"
    assert validate_live_confirmation(args(max_total_amount=999), [config()]) == "live_plan_exceeds_max_total_amount"


def test_live_confirmation_supports_multiple_csv_rows_with_total_limit():
    configs = [config("600001", 1000), config("000001", 1000)]
    assert validate_live_confirmation(args(max_order_amount=1000, max_total_amount=2000), configs) == ""
    assert validate_live_confirmation(args(max_order_amount=1000, max_total_amount=1999), configs) == "live_plan_exceeds_max_total_amount"
