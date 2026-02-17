#!/usr/bin/env python3
"""
Restore verified trades from the archived log.

Takes the 40 balance-verified trades (usdc_balance > 0) from the archive
and merges them with any FOK-verified trades from the current log.
Result: a clean longshot_trades.csv with only confirmed fills.
"""

import csv
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ARCHIVE = DATA_DIR / "archive_longshot_phantom_trades.csv"
CURRENT = DATA_DIR / "longshot_trades.csv"
OUTPUT = DATA_DIR / "longshot_trades.csv"
BACKUP = DATA_DIR / "longshot_trades_pre_restore.csv"

FIELDNAMES = [
    "timestamp", "market_slug", "coin", "timeframe", "side",
    "entry_price", "bet_size_usdc", "num_tokens", "outcome",
    "payout", "pnl", "bankroll", "usdc_balance", "btc_price",
    "other_side_price", "volatility_std",
]


def load_csv(path):
    if not path.exists():
        return []
    trades = []
    with open(path) as f:
        for row in csv.DictReader(f):
            trades.append(row)
    return trades


def main():
    # Load archive
    archive_trades = load_csv(ARCHIVE)
    if not archive_trades:
        print(f"ERROR: No trades in {ARCHIVE}")
        sys.exit(1)

    # Sort archive by timestamp
    archive_trades.sort(key=lambda t: t["timestamp"])

    # Extract only trades with verified USDC balance (> 0)
    verified = [t for t in archive_trades if float(t.get("usdc_balance", 0)) > 0]

    print(f"Archive total: {len(archive_trades)} trades")
    print(f"Verified (usdc_balance > 0): {len(verified)} trades")

    # Load current FOK trades
    current_trades = load_csv(CURRENT)
    print(f"Current FOK trades: {len(current_trades)} trades")

    # Find the latest timestamp in verified trades to avoid duplicates
    if verified:
        last_verified_ts = max(t["timestamp"] for t in verified)
        print(f"Last verified trade: {last_verified_ts[:19]}")
    else:
        last_verified_ts = ""

    # Filter current trades to only include those AFTER the verified period
    new_fok = [t for t in current_trades if t["timestamp"] > last_verified_ts]
    print(f"New FOK trades (after verified period): {len(new_fok)} trades")

    # Merge
    merged = verified + new_fok
    merged.sort(key=lambda t: t["timestamp"])

    print(f"\nMerged total: {len(merged)} trades")

    # Stats
    wins = sum(1 for t in merged if t["outcome"] == "won")
    losses = sum(1 for t in merged if t["outcome"] == "lost")
    pending = sum(1 for t in merged if t["outcome"] not in ("won", "lost"))
    total_pnl = sum(float(t["pnl"]) for t in merged)

    print(f"  Wins: {wins}  Losses: {losses}  Pending: {pending}")
    if (wins + losses) > 0:
        print(f"  Win rate: {wins / (wins + losses) * 100:.1f}%")
    print(f"  Total PnL: ${total_pnl:+.2f}")

    # Verification: bankroll vs USDC gap at end of period
    # NOTE: Simple (start_balance + PnL = end_balance) doesn't work for mid-stream
    # subsets because some positions were opened before the subset started but
    # settled within it. Instead we verify the bankroll-USDC gap is near zero
    # at the end, which confirms no phantom positions exist.
    if merged:
        last_bankroll = float(merged[-1].get("bankroll", 0))
        last_usdc = float(merged[-1].get("usdc_balance", 0))
        end_gap = abs(last_bankroll - last_usdc)

        print(f"\n  Last bankroll: ${last_bankroll:.2f}")
        print(f"  Last USDC:     ${last_usdc:.2f}")
        print(f"  Gap:           ${end_gap:.2f}")

        if end_gap < 3.0:
            print(f"  Integrity check: PASS (bankroll matches USDC)")
        else:
            print(f"  Integrity check: FAIL (gap ${end_gap:.2f}) — investigate")
            print(f"  NOT writing output. Fix the issue first.")
            sys.exit(1)

    # Backup current file
    if CURRENT.exists():
        import shutil
        shutil.copy2(CURRENT, BACKUP)
        print(f"\nBacked up current file to {BACKUP.name}")

    # Write merged output
    with open(OUTPUT, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for t in merged:
            writer.writerow({k: t.get(k, "") for k in FIELDNAMES})

    print(f"Wrote {len(merged)} trades to {OUTPUT.name}")
    print("DONE.")


if __name__ == "__main__":
    main()
