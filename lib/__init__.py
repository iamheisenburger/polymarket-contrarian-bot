"""
Lib - Reusable Components Library

This package provides reusable components for trading strategies and applications:

- console: Terminal output utilities (colors, formatting)
- market_manager: Market discovery and WebSocket management
- price_tracker: Price history and pattern detection
- position_manager: Position tracking with TP/SL
- trade_logger: CSV trade logging and analytics
- volatility_tracker: Rolling price volatility

Usage:
    from lib import MarketManager, PriceTracker, PositionManager
    from lib.console import Colors, print_colored
    from lib.trade_logger import TradeLogger
    from lib.volatility_tracker import VolatilityTracker
"""

from lib.console import Colors
from lib.market_manager import MarketManager, MarketInfo
from lib.price_tracker import PriceTracker, PricePoint, FlashCrashEvent
from lib.position_manager import PositionManager, Position
from lib.trade_logger import TradeLogger
from lib.volatility_tracker import VolatilityTracker

__all__ = [
    "Colors",
    "MarketManager",
    "MarketInfo",
    "PriceTracker",
    "PricePoint",
    "FlashCrashEvent",
    "PositionManager",
    "Position",
    "TradeLogger",
    "VolatilityTracker",
]
