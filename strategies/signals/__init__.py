"""Arena signal strategies — 10 independent trading signals."""

from .s01_xrp_specialist import S01_XRP_Specialist
from .s02_contrarian_fade import S02_Contrarian_Fade
from .s03_momentum_burst import S03_Momentum_Burst
from .s04_orderbook_imbalance import S04_OB_Imbalance
from .s05_dual_source import S05_Dual_Source
from .s06_tight_spread import S06_Tight_Spread
from .s07_early_bird import S07_Early_Bird
from .s08_consensus import S08_Consensus
from .s09_last_minute import S09_Last_Minute
from .s10_vol_regime import S10_Vol_Regime

ALL_SIGNALS = [
    S01_XRP_Specialist,
    S02_Contrarian_Fade,
    S03_Momentum_Burst,
    S04_OB_Imbalance,
    S05_Dual_Source,
    S06_Tight_Spread,
    S07_Early_Bird,
    S08_Consensus,
    S09_Last_Minute,
    S10_Vol_Regime,
]
