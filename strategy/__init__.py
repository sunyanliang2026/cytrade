"""Strategy package exports."""

from .base import BaseStrategy
from .csv_signal_strategy import CsvSignalStrategy
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
    if name == "JuejinSellStrategy":
        from .juejin_sell_strategy import JuejinSellStrategy as _JuejinSellStrategy

        return _JuejinSellStrategy
    if name == "MainSealFollowStrategy":
        from .main_seal_follow_strategy import MainSealFollowStrategy as _MainSealFollowStrategy

        return _MainSealFollowStrategy
    raise AttributeError(name)
