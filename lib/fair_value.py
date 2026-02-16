"""
Binary Option Fair Value Calculator

Calculates the theoretical fair probability for Polymarket binary crypto markets
using the current spot price from Binance and realized volatility.

Model: For a binary option paying $1 if S(T) > K:
    P(Up) = Φ( ln(S/K) / (σ√T) )

where:
    S = current spot price (from Binance)
    K = strike price (BTC price when the market opened)
    σ = annualized realized volatility
    T = time to expiry in years
    Φ = standard normal CDF

Usage:
    from lib.fair_value import BinaryFairValue

    fv = BinaryFairValue()
    fair_up = fv.calculate(
        spot=97500.0,
        strike=97400.0,
        seconds_to_expiry=600,
        volatility=0.50,
    )
    fair_down = 1.0 - fair_up
"""

import math
from dataclasses import dataclass

# Seconds in a year
SECONDS_PER_YEAR = 365.25 * 24 * 3600

# Price bounds — never quote outside these
MIN_PRICE = 0.01
MAX_PRICE = 0.99


def normal_cdf(x: float) -> float:
    """Standard normal CDF using the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class FairValue:
    """Fair value result for a binary market."""
    fair_up: float      # Probability that price goes UP
    fair_down: float    # Probability that price goes DOWN
    d: float            # The d-statistic (moneyness in vol units)
    spot: float         # Current spot price used
    strike: float       # Strike price used
    seconds_left: float # Time to expiry
    vol: float          # Volatility used

    @property
    def edge_up(self) -> float:
        """How far Up is from 50/50."""
        return self.fair_up - 0.5

    @property
    def dominant_side(self) -> str:
        """Which side is more likely."""
        return "up" if self.fair_up >= 0.5 else "down"

    @property
    def dominant_prob(self) -> float:
        """Probability of the dominant side."""
        return max(self.fair_up, self.fair_down)


class BinaryFairValue:
    """
    Fair value calculator for binary crypto options.

    Uses Black-Scholes-style pricing adapted for binary (digital) options
    on crypto assets.
    """

    def __init__(self, min_vol: float = 0.10, max_vol: float = 2.0):
        self.min_vol = min_vol
        self.max_vol = max_vol

    def calculate(
        self,
        spot: float,
        strike: float,
        seconds_to_expiry: float,
        volatility: float,
    ) -> FairValue:
        """
        Calculate fair value for a binary Up/Down market.

        Args:
            spot: Current price of the underlying (from Binance)
            strike: Strike price (price at market creation)
            seconds_to_expiry: Seconds until market resolves
            volatility: Annualized realized volatility (e.g., 0.50 = 50%)

        Returns:
            FairValue with fair_up and fair_down probabilities
        """
        # Clamp volatility
        vol = max(self.min_vol, min(self.max_vol, volatility))

        # Handle edge cases
        if seconds_to_expiry <= 0:
            # Expired: deterministic outcome
            fair_up = 1.0 if spot > strike else 0.0 if spot < strike else 0.5
            return FairValue(
                fair_up=fair_up, fair_down=1.0 - fair_up,
                d=float("inf") if spot > strike else float("-inf"),
                spot=spot, strike=strike, seconds_left=0, vol=vol,
            )

        if spot <= 0 or strike <= 0:
            return FairValue(
                fair_up=0.5, fair_down=0.5, d=0.0,
                spot=spot, strike=strike, seconds_left=seconds_to_expiry, vol=vol,
            )

        # Time in years
        T = seconds_to_expiry / SECONDS_PER_YEAR

        # d-statistic: how far spot is from strike in volatility units
        # For binary option: d = ln(S/K) / (σ√T)
        # We omit the drift term (r - σ²/2) since for short timeframes it's negligible
        sigma_sqrt_t = vol * math.sqrt(T)

        if sigma_sqrt_t < 1e-10:
            # Extremely low vol or time: essentially deterministic
            fair_up = 1.0 if spot > strike else 0.0 if spot < strike else 0.5
            d = 0.0
        else:
            d = math.log(spot / strike) / sigma_sqrt_t
            fair_up = normal_cdf(d)

        # Clamp to tradable range
        fair_up = max(MIN_PRICE, min(MAX_PRICE, fair_up))
        fair_down = max(MIN_PRICE, min(MAX_PRICE, 1.0 - fair_up))

        return FairValue(
            fair_up=fair_up,
            fair_down=fair_down,
            d=d,
            spot=spot,
            strike=strike,
            seconds_left=seconds_to_expiry,
            vol=vol,
        )

    def required_move_for_edge(
        self,
        seconds_to_expiry: float,
        volatility: float,
        target_prob: float = 0.65,
    ) -> float:
        """
        Calculate what % BTC move is needed to reach a target probability.

        Useful for understanding how sensitive the market is.

        Args:
            seconds_to_expiry: Seconds until expiry
            volatility: Annualized vol
            target_prob: Desired probability (e.g., 0.65)

        Returns:
            Required % price move (e.g., 0.002 = 0.2%)
        """
        from scipy.stats import norm  # Only needed for inverse CDF

        T = seconds_to_expiry / SECONDS_PER_YEAR
        sigma_sqrt_t = volatility * math.sqrt(T)
        d_required = norm.ppf(target_prob)

        # ln(S/K) = d * σ√T → S/K = exp(d * σ√T)
        price_ratio = math.exp(d_required * sigma_sqrt_t)
        return price_ratio - 1.0
