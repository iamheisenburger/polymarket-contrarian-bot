"""
DEPRECATED — Strategy Optimizer has been replaced by CoinManager.

The optimizer was over-engineered and caused losses by relaxing filters.
Use lib/coin_manager.py instead, which keeps strategy filters FIXED
and only adapts WHICH coins trade live vs paper.

See: lib/coin_manager.py, apps/run_coin_manager.py, scripts/auto_manage.sh
"""

# Re-export CoinManager for backwards compatibility
from lib.coin_manager import CoinManager

__all__ = ['CoinManager']
