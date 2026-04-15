# Bot Guide

**This file used to contain a Black-Scholes / fair-value strategy explanation. That entire system has been DELETED from the codebase. See [CLAUDE.md](CLAUDE.md) for the current strategy and the hard rule against re-introducing it.**

## Current strategy (one paragraph)

The bot trades Polymarket crypto 5-minute binary markets (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE) using a pure momentum + time-window strategy. On every cycle it computes `displacement = (spot - strike) / strike` per coin. If `|displacement| >= min_momentum`, the elapsed time is in `[min_window_elapsed, max_window_elapsed]`, and the entry price is in `[min_entry_price, max_entry_price]`, it fires on the side matching the displacement direction (`up` if `displacement > 0`, else `down`). Hold to settlement. No fair value model, no Black-Scholes, no Vatic, no leader filter, no window-direction lock. All 7 coins fire independently.

## Operational quick-reference

- **Dublin SSH:** `ssh -i ./polymarket-dublin.pem ubuntu@54.229.144.175`
- **Code dir:** `/opt/polymarket-bot/`
- **Live trade log:** `/opt/polymarket-bot/data/live_v30.csv`
- **Shadow log:** `/opt/polymarket-bot/data/shadow_v30.csv`
- **Pause:** `touch /opt/polymarket-bot/data/.bot_paused`
- **Resume:** `cd /opt/polymarket-bot && ./start_v30.sh` (removes pause flag and starts fresh process)
- **Process check:** `ps aux | grep run_sniper`

## What is permanently forbidden

See [CLAUDE.md](CLAUDE.md) — the HARD RULE section. In short: no fair value, no Black-Scholes, no Vatic, no min_edge filter, no min_fair_value config field, no probability-model-based trade selection. If a future session is tempted to add any of these "as a sanity check," they MUST stop and re-read the rule. The user has been emphatic across many sessions: this is bias, and bias has cost real money.
