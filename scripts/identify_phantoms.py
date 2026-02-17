#!/usr/bin/env python3
"""
Identify phantom trades in the archived longshot log.

Phantom trade = GTC order accepted by CLOB but never filled.
Bot tracked it as a position. On settlement, it logged a win/loss
that never actually happened. No real USDC moved.

Detection method:
- Each trade logs usdc_balance (actual on-chain USDC at log time).
- For trades where usdc_balance > 0, we can trace balance changes.
- A real BUY costs ~$1.05. A real WIN pays $5.00. A real LOSS costs $0 at resolution.
- Phantom trades show NO balance movement where we'd expect one.

Math behind the gap:
- Phantom WIN: logged +$3.95 PnL but actual USDC didn't change. Gap widens by $3.95.
- Phantom LOSS: logged -$1.05 PnL but actual USDC didn't change. Gap narrows by $1.05.
- Net gap formula: gap = (phantom_wins * 3.95) - (phantom_losses * 1.05)
"""

import csv
import sys
from pathlib import Path
from collections import defaultdict

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "archive_longshot_phantom_trades.csv"

def main():
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found")
        sys.exit(1)

    trades = []
    with open(CSV_PATH) as f:
        for row in csv.DictReader(f):
            trades.append(row)

    # Sort by timestamp
    trades.sort(key=lambda t: t["timestamp"])

    print(f"Total trades in archive: {len(trades)}")
    print()

    # === Section 1: Overall PnL vs Balance Gap ===
    total_pnl = sum(float(t["pnl"]) for t in trades)
    wins = sum(1 for t in trades if t["outcome"] == "won")
    losses = sum(1 for t in trades if t["outcome"] == "lost")
    pending = sum(1 for t in trades if t["outcome"] not in ("won", "lost"))

    print(f"=== OVERALL SUMMARY ===")
    print(f"Wins: {wins}  Losses: {losses}  Pending: {pending}")
    print(f"Win rate: {wins/(wins+losses)*100:.1f}%" if (wins+losses) > 0 else "N/A")
    print(f"Total logged PnL: ${total_pnl:+.2f}")
    print()

    # Find last trade with usdc_balance
    last_usdc = 0
    for t in reversed(trades):
        bal = float(t.get("usdc_balance", 0))
        if bal > 0:
            last_usdc = bal
            break

    print(f"Last recorded USDC balance: ${last_usdc:.2f}")
    print()

    # === Section 2: Trace balance where usdc_balance is available ===
    # The usdc_balance column started being populated partway through.
    # Find the first trade with a real usdc_balance.
    first_bal_idx = None
    for i, t in enumerate(trades):
        if float(t.get("usdc_balance", 0)) > 0:
            first_bal_idx = i
            break

    if first_bal_idx is None:
        print("ERROR: No trades have usdc_balance populated. Cannot trace.")
        sys.exit(1)

    print(f"=== BALANCE TRACKING ===")
    print(f"USDC balance starts at trade #{first_bal_idx} (0-indexed)")
    print(f"Trades WITHOUT balance data: {first_bal_idx}")
    print(f"Trades WITH balance data: {len(trades) - first_bal_idx}")
    print()

    # PnL for early trades (no balance data) — can't verify individually
    early_pnl = sum(float(trades[i]["pnl"]) for i in range(first_bal_idx))
    early_wins = sum(1 for i in range(first_bal_idx) if trades[i]["outcome"] == "won")
    early_losses = sum(1 for i in range(first_bal_idx) if trades[i]["outcome"] == "lost")

    print(f"--- Early trades (no balance data, trades 0-{first_bal_idx-1}) ---")
    print(f"  Wins: {early_wins}  Losses: {early_losses}")
    print(f"  Logged PnL: ${early_pnl:+.2f}")
    print()

    # PnL for later trades (with balance data)
    later_pnl = sum(float(trades[i]["pnl"]) for i in range(first_bal_idx, len(trades)))
    later_wins = sum(1 for i in range(first_bal_idx, len(trades)) if trades[i]["outcome"] == "won")
    later_losses = sum(1 for i in range(first_bal_idx, len(trades)) if trades[i]["outcome"] == "lost")

    print(f"--- Later trades (with balance data, trades {first_bal_idx}-{len(trades)-1}) ---")
    print(f"  Wins: {later_wins}  Losses: {later_losses}")
    print(f"  Logged PnL: ${later_pnl:+.2f}")
    print()

    # === Section 3: Group trades by market (same 15-min window) ===
    # Multiple trades in the same cycle complicate per-trade balance tracing.
    # Instead, look at balance snapshots between cycles.
    print(f"=== MARKET-BY-MARKET BALANCE TRACE ===")
    print()

    # Group by market_slug (each market is one 15-min cycle)
    markets = defaultdict(list)
    for i, t in enumerate(trades):
        markets[t["market_slug"]].append((i, t))

    # For each market, check if balance changes match expected
    suspicious = []

    # Sort markets by the timestamp of their first trade
    sorted_markets = sorted(markets.items(), key=lambda m: min(t[1]["timestamp"] for t in m[1]))

    prev_usdc = None
    for slug, market_trades in sorted_markets:
        # Get all USDC balances in this market
        usdcs = [float(t["usdc_balance"]) for _, t in market_trades if float(t.get("usdc_balance", 0)) > 0]

        if not usdcs:
            continue  # No balance data for this market

        usdc_at_market = usdcs[0]  # Balance snapshot during this market

        # Expected cost: sum of bet_size_usdc for buys in this market
        total_cost = sum(float(t["bet_size_usdc"]) for _, t in market_trades)

        # Expected payouts: sum of payouts
        total_payout = sum(float(t["payout"]) for _, t in market_trades)
        total_pnl_market = sum(float(t["pnl"]) for _, t in market_trades)

        # Check if bankroll vs usdc diverge
        bankrolls = [float(t["bankroll"]) for _, t in market_trades]
        bankroll_avg = sum(bankrolls) / len(bankrolls)
        usdc_avg = sum(usdcs) / len(usdcs) if usdcs else 0

        gap = bankroll_avg - usdc_avg

        if abs(gap) > 2.0:
            for idx, t in market_trades:
                suspicious.append({
                    "trade_idx": idx,
                    "timestamp": t["timestamp"][:19],
                    "coin": t["coin"],
                    "side": t["side"],
                    "outcome": t["outcome"],
                    "pnl": float(t["pnl"]),
                    "bankroll": float(t["bankroll"]),
                    "usdc": float(t["usdc_balance"]),
                    "gap": float(t["bankroll"]) - float(t["usdc_balance"]),
                    "slug": slug,
                })

    # === Section 4: Direct per-trade bankroll vs USDC gap ===
    print(f"=== PER-TRADE BANKROLL vs USDC GAP ===")
    print(f"{'#':>3} {'TIME':>19} {'COIN':<4} {'SIDE':<5} {'OUT':<5} {'PnL':>7} {'BANKROLL':>9} {'USDC':>9} {'GAP':>7}")
    print("-" * 80)

    gap_trades = []
    for i, t in enumerate(trades):
        usdc = float(t.get("usdc_balance", 0))
        if usdc <= 0:
            continue
        bankroll = float(t["bankroll"])
        gap = bankroll - usdc
        gap_trades.append((i, t, gap))
        flag = " <<<" if abs(gap) > 2.0 else ""
        print(f"{i:>3} {t['timestamp'][:19]} {t['coin']:<4} {t['side']:<5} {t['outcome']:<5} "
              f"${float(t['pnl']):>+6.2f} ${bankroll:>8.2f} ${usdc:>8.2f} ${gap:>+6.2f}{flag}")

    print()

    # === Section 5: Estimate phantom count ===
    if gap_trades:
        first_gap = gap_trades[0][2]
        last_gap = gap_trades[-1][2]
        gap_growth = last_gap - first_gap

        print(f"=== PHANTOM ESTIMATE ===")
        print(f"Gap at first balance-tracked trade: ${first_gap:+.2f}")
        print(f"Gap at last trade:                  ${last_gap:+.2f}")
        print(f"Gap growth during tracked period:   ${gap_growth:+.2f}")
        print()
        print(f"Estimated phantom WINS in tracked period: ~{abs(gap_growth)/3.95:.1f}")
        print(f"(Each phantom win inflates PnL by $3.95)")
        print()

        # Also check pre-tracking period
        # Starting balance was ~$23.91 (first trade bankroll)
        first_bankroll = float(trades[0]["bankroll"])
        first_tracked_usdc = float(gap_trades[0][1]["usdc_balance"])
        first_tracked_bankroll = float(gap_trades[0][1]["bankroll"])

        pre_track_pnl = sum(float(trades[i]["pnl"]) for i in range(gap_trades[0][0]))
        expected_usdc_at_track_start = first_bankroll + pre_track_pnl

        print(f"--- Pre-tracking period analysis ---")
        print(f"First trade bankroll: ${first_bankroll:.2f}")
        print(f"PnL during pre-tracking: ${pre_track_pnl:+.2f}")
        print(f"Expected USDC at tracking start: ${expected_usdc_at_track_start:.2f}")
        print(f"Actual USDC at tracking start: ${first_tracked_usdc:.2f}")
        pre_gap = expected_usdc_at_track_start - first_tracked_usdc
        print(f"Pre-tracking gap: ${pre_gap:+.2f}")
        if pre_gap > 2:
            print(f"Estimated phantom WINS in pre-tracking: ~{pre_gap/3.95:.1f}")
        print()

    # === Section 6: Summary ===
    total_gap = total_pnl - (last_usdc - first_bankroll) if last_usdc > 0 else 0
    print(f"=== FINAL ASSESSMENT ===")
    print(f"Starting bankroll (trade 0): ${float(trades[0]['bankroll']):.2f}")
    print(f"Total logged PnL:            ${total_pnl:+.2f}")
    print(f"Expected ending USDC:        ${float(trades[0]['bankroll']) + total_pnl:.2f}")
    print(f"Actual ending USDC:          ${last_usdc:.2f}")
    print(f"Total discrepancy:           ${total_gap:+.2f}")
    print()
    est_phantom_wins = round(total_gap / 3.95)
    print(f"Estimated total phantom wins: ~{est_phantom_wins}")
    print(f"Adjusted win count: {wins} - {est_phantom_wins} = {wins - est_phantom_wins}")
    print(f"Adjusted win rate: {(wins - est_phantom_wins)/(wins + losses - est_phantom_wins)*100:.1f}%")
    print(f"(Still compared to 21% breakeven)")
    print()


if __name__ == "__main__":
    main()
