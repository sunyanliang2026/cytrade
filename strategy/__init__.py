"""Strategy package exports."""

from .base import BaseStrategy
from .csv_signal_strategy import CsvSignalStrategy
from .juejin_sell_strategy import JuejinSellStrategy
from .models import StrategyConfig, StrategySnapshot
from .runner import StrategyRunner

__all__ = [
    "StrategyConfig",
    "StrategySnapshot",
    "BaseStrategy",
    "CsvSignalStrategy",
    "JuejinSellStrategy",
    "MainSealFollowStrategy",
    "StrategyRunner",
]


def __getattr__(name):
    if name == "MainSealFollowStrategy":
        from .main_seal_follow_strategy import MainSealFollowStrategy as _MainSealFollowStrategy

        return _MainSealFollowStrategy
    raise AttributeError(name)
