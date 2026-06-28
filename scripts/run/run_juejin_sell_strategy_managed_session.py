"""Compatibility wrapper for the JuejinSellStrategy managed-session runner."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.juejin_sell_strategy.scripts.run_managed_session import *  # noqa: F401,F403,E402
from strategies.juejin_sell_strategy.scripts.run_managed_session import main  # noqa: E402


if __name__ == "__main__":
    main()
