"""Compatibility wrapper for the relocated MainSealFollow managed-session runner."""

from strategies.main_seal_follow.scripts.run_managed_session import *  # noqa: F401,F403
from strategies.main_seal_follow.scripts.run_managed_session import main


if __name__ == "__main__":
    main()
