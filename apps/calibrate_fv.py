#!/usr/bin/env python3
"""
Calibrate Direct Fair Value Model — Fit sigmoid parameters from real trade data.

Reads trade CSV (paper or live), computes empirical win rates by
(price_gap, time_remaining) bucket, and fits the DirectFairValue
sigmoid model to match.

Usage:
    # Calibrate from paper trades
    python apps/calibrate_fv.py --trade-csv data/paper_3coin_5m.csv --output data/calibration.json

    # Calibrate from live trades
    python apps/calibrate_fv.py --trade-csv data/late_sniper_5m.csv --output data/calibration.json

    # Just print the calibration table (no save)
    python apps/calibrate_fv.py --trade-csv data/paper_3coin_5m.csv
"""

import sys
import argparse
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.direct_fv import DirectFairValue


def main():
    parser = argparse.ArgumentParser(
        description="Calibrate DirectFairValue model from trade data"
    )
    parser.add_argument(
        "--trade-csv", type=str, required=True,
        help="Path to trade CSV with spot, strike, tte, outcome, side columns"
    )
    parser.add_argument(
        "--output", type=str, default="",
        help="Output path for calibration JSON (default: print only, no save)"
    )
    parser.add_argument(
        "--compare", action="store_true",
        help="Compare DirectFV vs Black-Scholes predictions on the trade data"
    )
    args = parser.parse_args()

    csv_path = args.trade_csv
    if not Path(csv_path).exists():
        print(f"ERROR: Trade CSV not found: {csv_path}")
        sys.exit(1)

    print(f"Calibrating from: {csv_path}")
    print()

    fv = DirectFairValue()
    result = fv.calibrate(csv_path)

    if "error" in result:
        print(f"ERROR: {result['error']}")
        sys.exit(1)

    # Print summary
    print(f"Total trades: {result['total_trades']}")
    print(f"Overall WR:   {result['overall_wr']*100:.1f}%")
    print()

    # Print fitted parameters
    params = result["params"]
    print(f"Fitted parameters:")
    print(f"  k (steepness):      {params['k']:.1f}")
    print(f"  alpha (time decay):  {params['alpha']:.2f}")
    print(f"  ref_time:           {params['ref_time']:.0f}s")
    print()

    # Print calibration table
    buckets = result.get("buckets", [])
    if buckets:
        print(f"{'Gap Range':<18} {'Time Range':<12} {'Trades':>7} {'Wins':>6} {'WR':>7} {'Model':>7} {'Err':>7}")
        print("-" * 76)

        for row in buckets:
            # Calculate model prediction for bucket midpoint
            gap_mid = row["gap_mid"]
            time_mid = max(1.0, row["time_mid"])
            # Use a dummy spot/strike that gives the gap_mid
            dummy_strike = 100000.0
            dummy_spot = dummy_strike * (1 + gap_mid)
            model_fv = fv.calculate(dummy_spot, dummy_strike, time_mid)
            model_prob = model_fv.fair_up

            err = model_prob - row["win_rate"]
            print(f"{row['gap_range']:<18} {row['time_range']:<12} {row['trades']:>7} "
                  f"{row['wins']:>6} {row['win_rate']*100:>6.1f}% {model_prob*100:>6.1f}% {err*100:>+6.1f}%")

        print()

    # Compare with Black-Scholes if requested
    if args.compare:
        _compare_models(csv_path, fv)

    # Save if output path specified
    if args.output:
        fv.save_calibration(args.output, result)
        print(f"Saved calibration to: {args.output}")
    else:
        print("(Use --output <path> to save calibration to JSON)")


def _compare_models(csv_path: str, direct_fv: DirectFairValue):
    """Compare DirectFV vs Black-Scholes on the same trade data."""
    import csv
    from lib.fair_value import BinaryFairValue

    bs = BinaryFairValue()
    direct_correct = 0
    bs_correct = 0
    total = 0

    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                spot = float(row.get("spot") or row.get("binance_price") or row.get("spot_price", 0))
                strike = float(row.get("strike") or row.get("strike_price", 0))
                tte = float(row.get("seconds_left") or row.get("tte") or row.get("time_to_expiry", 0))
                side = (row.get("side", "") or "").lower()
                outcome = (row.get("outcome", "") or row.get("result", "")).lower()

                if outcome in ("win", "1", "true", "won"):
                    won = True
                elif outcome in ("loss", "0", "false", "lost", "lose"):
                    won = False
                else:
                    continue

                if spot <= 0 or strike <= 0 or tte <= 0:
                    continue

                # DirectFV prediction
                dfv = direct_fv.calculate(spot, strike, tte)
                d_prob = dfv.fair_up if side == "up" else dfv.fair_down
                d_predicted_win = d_prob > 0.5

                # Black-Scholes prediction (use 15% fixed vol)
                bfv = bs.calculate(spot, strike, tte, 0.15)
                b_prob = bfv.fair_up if side == "up" else bfv.fair_down
                b_predicted_win = b_prob > 0.5

                total += 1
                if d_predicted_win == won:
                    direct_correct += 1
                if b_predicted_win == won:
                    bs_correct += 1

            except (ValueError, TypeError):
                continue

    if total > 0:
        print(f"\nModel comparison on {total} trades:")
        print(f"  DirectFV accuracy:       {direct_correct}/{total} ({direct_correct/total*100:.1f}%)")
        print(f"  Black-Scholes accuracy:  {bs_correct}/{total} ({bs_correct/total*100:.1f}%)")
        print()


if __name__ == "__main__":
    main()
