# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⛔️ HARD RULE — NO FAIR VALUE, NO BLACK-SCHOLES, NO VATIC

**This is a non-negotiable user constraint repeated across many sessions.** Fair value calculation, Black-Scholes pricing, and Vatic strike fetching have been explicitly removed from the system. They must NOT be reintroduced under any circumstance.

If you find yourself about to:
- Add a `fair_value` calculation, `BinaryFairValue` class, or `_calculate_fair_value()` method → **STOP**
- Add a `min_edge` filter where `edge = fair_prob - ask` → **STOP**
- Add a `min_fair_value` config field → **STOP**
- Import `lib.fair_value`, `lib.vatic_client`, or `lib.direct_fv` → **STOP** (these files no longer exist)
- Add `--no-vatic`, `--require-vatic`, `--min-edge`, or `--min-fair-value` CLI flags → **STOP**
- Reference Black-Scholes anywhere → **STOP**

The bot is **pure momentum + TTE + entry-price filter only**. Side selection is by momentum direction (`spot > strike` → up, else down). No probability model. No expected-value comparison against a theoretical fair price. The only signal is "the underlying moved by X% within the last N seconds."

If a future session is tempted to "add a fair value sanity check" or "use Black-Scholes to filter out coin-flip trades" — DO NOT. The user has been emphatic: this is bias, and bias has cost real money. See `feedback_no_fair_value_no_vatic.md` in the auto-memory.

**Backtests must also be unbiased.** The polybacktest CSV columns are: `timestamp, market_slug, coin, side, momentum_direction, is_momentum_side, threshold, entry_price, best_bid, momentum, elapsed, outcome`. There is no fair_value column. Any backtest sweep must filter only on momentum / elapsed / entry_price / outcome. Never use a probability model to score trades retroactively.

## Active Strategy: Momentum Sniper

The bot trades Polymarket crypto binary 5-minute markets (BTC / ETH / SOL / XRP / DOGE / BNB / HYPE) by sniping price-momentum signals.

**The core idea (in plain English):**
1. At any moment in a 5-minute market window, compute `displacement = (spot_price - strike_price) / strike_price`
2. If `|displacement| >= min_momentum` (e.g. 0.0010 = 0.1%), AND the elapsed time is in the configured TTE window (e.g. 165-270 seconds into a 300-second window), AND the entry price is in the broad range (e.g. 0.05-0.99), then fire.
3. Fire on the **side matching the displacement direction**: `side = "up" if displacement > 0 else "down"`.
4. Hold to settlement. $1 per token if right, $0 if wrong.

**That's the entire strategy.** Three filters (momentum / TTE / entry-price), one direction rule. Nothing else.

| | Status |
|---|---|
| Currently | V30.5 deployed (post-cleanup), all 7 coins independent, no window-direction lock |
| Trade log | `data/live_v30.csv` |
| Shadow log | `data/shadow_v30.csv` |
| Strike source | Chainlink for BTC/ETH/SOL/XRP/DOGE/HYPE; Binance for BNB |
| Coin independence | All coins fire on their own momentum. No leader filter. HYPE has its own Coinbase price feed. |

## VPS & Deployment

**Dublin (bot):**
- **SSH:** `ssh -i ./polymarket-dublin.pem ubuntu@54.229.144.175`
- **Code:** `/opt/polymarket-bot/`
- **Trade log:** `data/live_v30.csv` | **Shadow log:** `data/shadow_v30.csv`
- The sniper runs as a background process (NOT systemd). Check with `ps aux | grep python`.
- Pause flag: `data/.bot_paused` (touch it to pause, rm to resume — or use `start_v30.sh` which removes it).

**Amsterdam (market recorder):**
- **SSH:** `ssh -i ~/.ssh/polymarket_bot root@134.209.91.109`
- **Code:** `/opt/polymarket-bot/`

**Wallet:**
- **EOA:** 0x788AdB6eaDa73377F7b63F59b2fF0573C86A65E5
- **Safe:** 0x13D0684C532be5323662e851a1Bd10DF46d79806
- **GitHub:** https://github.com/iamheisenburger/polymarket-contrarian-bot

## Common Commands

```bash
# Setup (first time)
pip install -r requirements.txt
cp .env.example .env  # Edit with your credentials
source .env

# Run the sniper bot locally (example V30.5 invocation)
python apps/run_sniper.py \
  --coins BTC ETH SOL XRP DOGE BNB HYPE \
  --timeframe 5m --bankroll 55 \
  --min-entry-price 0.05 --max-entry-price 0.99 \
  --min-momentum 0.0010 \
  --min-window-elapsed 180 --max-window-elapsed 270 \
  --max-concurrent-positions 7 \
  --min-size

# Run tests
pytest tests/ -v

# SSH quick checks
ssh -i ./polymarket-dublin.pem ubuntu@54.229.144.175 "tail -20 /opt/polymarket-bot/data/live_v30.csv"
```

## Architecture

```
apps/run_sniper.py              # CLI entry point — momentum + TTE + entry-price flags only
strategies/momentum_sniper.py   # Core strategy (SniperConfig, MomentumSniperStrategy)
lib/binance_ws.py               # Binance real-time price feed
lib/coinbase_ws.py              # Coinbase price feed (HYPE only)
lib/trade_logger.py             # CSV trade logging
lib/signal_logger.py            # Per-coin signal evaluation log
lib/shadow_logger.py            # Paper-vs-live tracking
lib/market_manager.py           # Market lifecycle management
src/bot.py                      # Order execution, balance, auto-redemption
src/client.py                   # Polymarket CLOB/Relayer API client
src/signer.py                   # EIP-712 order signing
src/websocket_client.py         # Polymarket orderbook WebSocket
src/gamma_client.py             # Market discovery
```

### Key Modules

| Module | Purpose | Key Classes |
|--------|---------|-------------|
| `momentum_sniper.py` | Core trading strategy | `MomentumSniperStrategy`, `SniperConfig` |
| `bot.py` | Order execution + redemption | `TradingBot`, `OrderResult` |
| `client.py` | API communication | `ClobClient`, `RelayerClient` |
| `signer.py` | EIP-712 signing | `OrderSigner`, `Order` |
| `trade_logger.py` | Trade CSV logging | `TradeLogger` |

### Data Flow
1. Binance / Coinbase WebSocket provides sub-second crypto price updates
2. Bot computes `displacement = (spot - strike) / strike` per coin per cycle
3. If `|displacement| >= min_momentum` AND elapsed in [min_window_elapsed, max_window_elapsed] AND ask in [min_entry_price, max_entry_price] → fire on the side matching the displacement direction
4. Hold to settlement. Binary pays $1 per token won, $0 lost.
5. Auto-redeem winnings every 3 minutes and compound into next market.

## Key Patterns

- **Async methods**: All trading operations are async
- **Config precedence**: Environment vars > YAML file > defaults
- **Builder HMAC auth**: Timestamp + method + path + body signed with api_secret
- **Signature type 2**: Gnosis Safe signatures for Polymarket
- **min_size_mode**: When True, always buys exactly 5 tokens (data collection mode)
- **HYPE exception**: HYPE uses Coinbase price feed (not on Binance spot) and has structurally lower correlation to BTC (~0.48 vs 0.85+ for other alts)

## Testing Notes

- Tests use `pytest` with `pytest-asyncio` for async
- Mock external API calls; never hit real Polymarket APIs in tests
- Test private key: `"0x" + "a" * 64`
- Test safe address: `"0x" + "b" * 40`

## Polymarket API Context

- CLOB API: `https://clob.polymarket.com` - order submission/cancellation
- Relayer API: `https://relayer-v2.polymarket.com` - gasless transactions
- Gamma API: `https://gamma-api.polymarket.com` - market discovery
- Token IDs are ERC-1155 identifiers for market outcomes
- Prices are 0-1 (probability percentages)
- USDC has 6 decimal places, minimum order $1.00 / 5 tokens
- Polymarket uses Chainlink price feeds for settlement

**Important**: The `docs/` directory contains official Polymarket documentation. When implementing or debugging API features, always reference:
- `docs/developers/CLOB/` - CLOB API endpoints, authentication, orders
- `docs/developers/builders/` - Builder Program, Relayer, gasless transactions
- `docs/api-reference/` - REST API endpoint specifications

## Retired Strategies
- **Contrarian** (`strategies/contrarian.py`): Bought cheap side ($0.03-$0.07) of BTC 5m markets. RETIRED.
- **Flash Crash** (`strategies/flash_crash.py`): Volatility-based probability drop trading. UNUSED.
- **Market Maker** (`strategies/market_maker.py`): Two-sided quoting. RETIRED — used to import `lib.fair_value` which has been deleted, so this file will not run as-is. Do not resurrect without removing all fair-value dependencies first.
- **Weather Edge** (`strategies/weather_edge.py`): Multi-model consensus. RETIRED.
- **Paper Arena** (`strategies/paper_arena.py`): Paper trading harness. RETIRED.

## Files explicitly REMOVED in the no-fair-value cleanup
- `lib/fair_value.py` (Black-Scholes binary pricing) — DELETED
- `lib/vatic_client.py` (Vatic strike fetcher) — DELETED
- `lib/direct_fv.py` (empirical fair-value model) — DELETED
- `apps/calibrate_fv.py` (calibration tool) — DELETED

If you find references to any of these in surviving code, delete them. They are not coming back.
