"""Run MainSealFollowStrategy with local runtime configuration."""

import os

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from main import run_scheduler_service
from strategy.main_seal_follow_strategy import MainSealFollowStrategy


if __name__ == "__main__":
    run_scheduler_service(strategy_classes=[MainSealFollowStrategy])
