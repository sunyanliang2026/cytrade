"""Compatibility wrapper for the relocated MainSealFollow market-only runner."""

from strategies.main_seal_follow.scripts.run_market_only import *  # noqa: F401,F403
from strategies.main_seal_follow.scripts.run_market_only import run_market_only


if __name__ == "__main__":
    run_market_only()
