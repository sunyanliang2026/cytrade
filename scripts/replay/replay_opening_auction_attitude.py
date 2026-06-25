"""Compatibility wrapper for the relocated OpeningAuctionAttitude replay tool."""

from strategies.opening_auction_attitude.scripts.replay_session import *  # noqa: F401,F403
from strategies.opening_auction_attitude.scripts.replay_session import main


if __name__ == "__main__":
    main()
