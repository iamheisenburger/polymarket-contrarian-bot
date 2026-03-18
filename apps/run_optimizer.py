#!/usr/bin/env python3
"""
DEPRECATED — Strategy Optimizer has been replaced by Coin Manager.

Use apps/run_coin_manager.py instead.

The optimizer was over-engineered and caused losses by relaxing filters.
The coin manager keeps strategy filters FIXED and only adapts which
coins trade live vs paper.
"""

import sys
print("DEPRECATED: Use apps/run_coin_manager.py instead.")
print("The optimizer has been replaced by the coin manager.")
print("Run: python apps/run_coin_manager.py --help")
sys.exit(1)
