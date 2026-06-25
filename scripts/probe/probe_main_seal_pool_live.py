"""Compatibility wrapper for the relocated MainSealFollow probe tool."""

from strategies.main_seal_follow.scripts.probe_pool_live import *  # noqa: F401,F403
from strategies.main_seal_follow.scripts.probe_pool_live import main


if __name__ == "__main__":
    main()
