"""Compatibility wrapper for the relocated OpeningAuctionAttitude replay tool."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.opening_auction_attitude.scripts.replay_session import *  # noqa: F401,F403,E402
from strategies.opening_auction_attitude.scripts.replay_session import main  # noqa: E402


if __name__ == "__main__":
    main()
