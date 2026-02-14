"""
Strategies - Trading Strategy Implementations

This package contains trading strategy implementations:

- base: Base class for all strategies
- flash_crash: Flash crash volatility strategy
- contrarian: Contrarian cheap-side strategy

Usage:
    from strategies.base import BaseStrategy, StrategyConfig
    from strategies.flash_crash import FlashCrashStrategy, FlashCrashConfig
    from strategies.contrarian import ContrarianStrategy, ContrarianConfig
"""

from strategies.base import BaseStrategy, StrategyConfig
from strategies.flash_crash import FlashCrashStrategy, FlashCrashConfig
from strategies.contrarian import ContrarianStrategy, ContrarianConfig

__all__ = [
    "BaseStrategy",
    "StrategyConfig",
    "FlashCrashStrategy",
    "FlashCrashConfig",
    "ContrarianStrategy",
    "ContrarianConfig",
]
