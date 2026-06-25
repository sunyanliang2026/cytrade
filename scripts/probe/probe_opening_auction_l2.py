"""Compatibility wrapper for the relocated OpeningAuctionAttitude L2 probe."""

from strategies.opening_auction_attitude.scripts.probe_l2 import *  # noqa: F401,F403
from strategies.opening_auction_attitude.scripts.probe_l2 import main


if __name__ == "__main__":
    main()
