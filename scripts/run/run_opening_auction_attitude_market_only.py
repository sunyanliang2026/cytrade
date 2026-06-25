"""Compatibility wrapper for the relocated OpeningAuctionAttitude market-only runner."""

from strategies.opening_auction_attitude.scripts.run_market_only import *  # noqa: F401,F403
from strategies.opening_auction_attitude.scripts.run_market_only import main


if __name__ == "__main__":
    main()
