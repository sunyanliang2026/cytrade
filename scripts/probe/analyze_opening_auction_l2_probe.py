"""Compatibility wrapper for the relocated OpeningAuctionAttitude L2 analysis tool."""

from strategies.opening_auction_attitude.scripts.analyze_l2_probe import *  # noqa: F401,F403
from strategies.opening_auction_attitude.scripts.analyze_l2_probe import main


if __name__ == "__main__":
    main()
