#!/usr/bin/env python3
"""
Test fair value calculations against real Polymarket data.

Fetches the current BTC 15-min market, compares our model's fair value
to the actual market price, and validates the Binance price feed.

Usage:
    python scripts/test_fair_value.py
    python scripts/test_fair_value.py --coin ETH
"""

import sys
import time
import math
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.fair_value import BinaryFairValue, normal_cdf
from lib.binance_ws import BinancePriceFeed
from src.gamma_client import GammaClient


def test_math():
    """Test the fair value math with known scenarios."""
    fv = BinaryFairValue()

    print("=" * 60)
    print("  FAIR VALUE MODEL TESTS")
    print("=" * 60)
    print()

    # Test 1: At-the-money, should be ~50%
    result = fv.calculate(spot=97000, strike=97000, seconds_to_expiry=900, volatility=0.50)
    print(f"  Test 1: ATM (S=K=$97,000, T=15min, vol=50%)")
    print(f"    Fair Up:   {result.fair_up:.4f}  (expected ~0.50)")
    print(f"    Fair Down: {result.fair_down:.4f}")
    assert 0.48 < result.fair_up < 0.52, f"ATM should be ~0.50, got {result.fair_up}"
    print(f"    PASS")
    print()

    # Test 2: BTC moved up 0.2% with 10 min left
    result = fv.calculate(spot=97194, strike=97000, seconds_to_expiry=600, volatility=0.50)
    print(f"  Test 2: Up 0.2% (S=$97,194 K=$97,000, T=10min, vol=50%)")
    print(f"    Fair Up:   {result.fair_up:.4f}  (should be >0.60)")
    print(f"    d-stat:    {result.d:.3f}")
    assert result.fair_up > 0.55, f"0.2% up should give >0.55, got {result.fair_up}"
    print(f"    PASS")
    print()

    # Test 3: BTC moved down 0.5% with 5 min left
    result = fv.calculate(spot=96515, strike=97000, seconds_to_expiry=300, volatility=0.50)
    print(f"  Test 3: Down 0.5% (S=$96,515 K=$97,000, T=5min, vol=50%)")
    print(f"    Fair Up:   {result.fair_up:.4f}  (should be <0.15)")
    print(f"    Fair Down: {result.fair_down:.4f}")
    assert result.fair_up < 0.20, f"0.5% down with 5min should give <0.20, got {result.fair_up}"
    print(f"    PASS")
    print()

    # Test 4: Near expiry, small move
    result = fv.calculate(spot=97050, strike=97000, seconds_to_expiry=30, volatility=0.50)
    print(f"  Test 4: Near expiry (S=$97,050 K=$97,000, T=30s, vol=50%)")
    print(f"    Fair Up:   {result.fair_up:.4f}  (should be very high)")
    print(f"    d-stat:    {result.d:.3f}")
    assert result.fair_up > 0.80, f"Above strike near expiry should give >0.80, got {result.fair_up}"
    print(f"    PASS")
    print()

    # Test 5: Already expired, above strike
    result = fv.calculate(spot=97100, strike=97000, seconds_to_expiry=0, volatility=0.50)
    print(f"  Test 5: Expired above strike (S=$97,100 K=$97,000, T=0)")
    print(f"    Fair Up:   {result.fair_up:.4f}  (should be 1.00)")
    assert result.fair_up == 1.0, f"Expired above strike should be 1.0, got {result.fair_up}"
    print(f"    PASS")
    print()

    # Test 6: Sensitivity table
    print(f"  Sensitivity: BTC move vs Fair Up (T=10min, vol=50%)")
    print(f"  {'Move':>8s}  {'Spot':>10s}  {'Fair Up':>8s}  {'Fair Down':>9s}")
    print(f"  {'-'*8}  {'-'*10}  {'-'*8}  {'-'*9}")
    for pct in [-0.5, -0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3, 0.5]:
        spot = 97000 * (1 + pct / 100)
        r = fv.calculate(spot=spot, strike=97000, seconds_to_expiry=600, volatility=0.50)
        print(f"  {pct:+7.1f}%  ${spot:>9,.0f}  {r.fair_up:>8.4f}  {r.fair_down:>9.4f}")
    print()

    print(f"  All math tests PASSED")
    print()


async def test_live_data(coin: str = "BTC"):
    """Test against real Polymarket + Binance data."""
    print("=" * 60)
    print(f"  LIVE DATA TEST — {coin} 15m")
    print("=" * 60)
    print()

    # Start Binance feed
    print("  Starting Binance price feed...")
    feed = BinancePriceFeed(coins=[coin])
    await feed.start()

    spot = feed.get_price(coin)
    vol = feed.get_volatility(coin)
    print(f"  Binance {coin}: ${spot:,.2f}")
    print(f"  Realized vol: {vol*100:.1f}% (annualized)")
    print()

    # Get current Polymarket market
    print("  Fetching Polymarket market...")
    gamma = GammaClient()
    market = gamma.get_current_market(coin, "15m")

    if not market:
        print(f"  No active 15m market found for {coin}")
        await feed.stop()
        return

    slug = market.get("slug", "?")
    prices = gamma.parse_prices(market)
    mkt_up = prices.get("up", 0.5)
    mkt_down = prices.get("down", 0.5)

    print(f"  Market: {slug}")
    print(f"  Market Up:   {mkt_up:.4f}")
    print(f"  Market Down: {mkt_down:.4f}")
    print()

    # Parse market timing
    try:
        ts_str = slug.rsplit("-", 1)[-1]
        market_start_ts = int(ts_str)
        market_end_ts = market_start_ts + 900
        secs_left = max(0, market_end_ts - time.time())
        secs_elapsed = 900 - secs_left
    except (ValueError, IndexError):
        secs_left = 600
        secs_elapsed = 300

    print(f"  Time elapsed: {secs_elapsed:.0f}s | Time left: {secs_left:.0f}s")
    print()

    # Calculate fair value
    # Strike estimation: back-solve from market price
    fv_calc = BinaryFairValue()

    # Method 1: Use current spot as strike (naive)
    fv_naive = fv_calc.calculate(spot=spot, strike=spot, seconds_to_expiry=secs_left, volatility=vol)
    print(f"  Fair value (strike=spot): Up={fv_naive.fair_up:.4f} Down={fv_naive.fair_down:.4f}")

    # Method 2: Back-solve strike from market price
    if 0.1 < mkt_up < 0.9 and vol > 0 and secs_left > 30:
        T = secs_left / (365.25 * 24 * 3600)
        sigma_sqrt_t = vol * math.sqrt(T)

        # Approximate inverse normal CDF
        def approx_inv_normal(p):
            if p <= 0.0 or p >= 1.0:
                return 0.0
            if p < 0.5:
                return -approx_inv_normal(1.0 - p)
            t = math.sqrt(-2.0 * math.log(1.0 - p))
            c0, c1, c2 = 2.515517, 0.802853, 0.010328
            d1, d2, d3 = 1.432788, 0.189269, 0.001308
            return t - (c0 + c1 * t + c2 * t * t) / (1 + d1 * t + d2 * t * t + d3 * t * t * t)

        d_implied = approx_inv_normal(mkt_up)
        implied_strike = spot * math.exp(-d_implied * sigma_sqrt_t)

        fv_implied = fv_calc.calculate(
            spot=spot, strike=implied_strike,
            seconds_to_expiry=secs_left, volatility=vol
        )

        print(f"  Implied strike: ${implied_strike:,.2f}")
        print(f"  Fair value (implied strike): Up={fv_implied.fair_up:.4f} Down={fv_implied.fair_down:.4f}")
        print()

        # Compare our fair value to market
        edge = fv_implied.fair_up - mkt_up
        print(f"  Edge (fair - market): {edge:+.4f}")
        if abs(edge) < 0.02:
            print(f"  Market is EFFICIENT (edge < 2 cents)")
        else:
            side = "UP" if edge > 0 else "DOWN"
            print(f"  Potential opportunity: {side} is underpriced by {abs(edge):.2f}")
    else:
        print(f"  Market too extreme or too close to expiry for strike estimation")

    print()

    # Show what spread would capture
    print(f"  Market Making Economics (at 4-cent half-spread):")
    print(f"    Quote Up at:   {mkt_up - 0.04:.2f} (buy below market)")
    print(f"    Quote Down at: {mkt_down - 0.04:.2f} (buy below market)")
    total = (mkt_up - 0.04) + (mkt_down - 0.04)
    if total < 1.0:
        print(f"    If both fill:  ${total:.2f} cost -> $1.00 payout = ${1.0-total:.2f} profit")
    else:
        print(f"    Both fill cost ${total:.2f} > $1.00 — would need wider spread")
    print()

    # Wait and show price movement
    print("  Watching for 15 seconds...")
    for i in range(3):
        await asyncio.sleep(5)
        new_spot = feed.get_price(coin)
        new_vol = feed.get_volatility(coin)
        pct_move = (new_spot - spot) / spot * 100
        age = feed.get_age(coin)
        print(f"    +{(i+1)*5}s: ${new_spot:,.2f} ({pct_move:+.4f}%) vol={new_vol*100:.1f}% age={age:.1f}s")

    await feed.stop()
    print()
    print("  Done!")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Test fair value calculations")
    parser.add_argument("--coin", default="BTC", choices=["BTC", "ETH", "SOL"])
    parser.add_argument("--skip-live", action="store_true", help="Skip live data test")
    args = parser.parse_args()

    # Run math tests
    test_math()

    # Run live test
    if not args.skip_live:
        asyncio.run(test_live_data(args.coin))


if __name__ == "__main__":
    main()
