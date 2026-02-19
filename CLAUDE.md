# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Active Strategy: Momentum Sniper (Longshot + Midshot)

**READ `BOT_GUIDE.md` FIRST** — it has the full strategy breakdown, math, master plan, SSH commands, and architecture.

This bot trades Polymarket crypto binary markets (BTC/ETH/SOL/XRP Up/Down, 15m) by
exploiting Binance-to-Polymarket latency. Black-Scholes calculates fair value; when
market price lags, we buy the mispriced side and hold to settlement ($1 win / $0 loss).

Two campaigns using the same engine (`strategies/momentum_sniper.py`):

| | Longshot | Midshot |
|---|---------|---------|
| Status | **LIVE** (data collection) | NOT YET LIVE |
| Entry price | < $0.20 | ~$0.40-$0.60 |
| Payout | 4.76x | ~2x |
| Breakeven WR | 21% | 50% |
| Trade log | `data/longshot_trades.csv` | `data/midshot_trades.csv` (TBD) |

**Master Plan (7 steps):**
1. [CURRENT] Collect 50 trades/coin on Longshot (200 total)
2. If profitable, remove `--min-size` and let Kelly scale
3. Grow account
4. Start Midshot at `--min-size` (50 trades/coin)
5. Evaluate Midshot profitability
6. If profitable, remove `--min-size` on Midshot
7. Explore 5m markets

**IMPORTANT:** No strategy changes during data collection. The CSV trade log is ground truth.

## VPS & Deployment

- **IP:** 209.38.36.107 | **SSH:** `ssh -i ~/.ssh/polymarket_bot root@209.38.36.107`
- **Code:** `/opt/polymarket-bot/` | **GitHub:** https://github.com/iamheisenburger/polymarket-contrarian-bot
- **Sniper log:** `/var/log/polymarket-sniper.log`
- **Wallet EOA:** 0x788AdB6eaDa73377F7b63F59b2fF0573C86A65E5
- **Safe address:** 0x13D0684C532be5323662e851a1Bd10DF46d79806

The sniper runs as a background process (NOT systemd). Check with `ps aux | grep python`.

## Common Commands

```bash
# Setup (first time)
pip install -r requirements.txt
cp .env.example .env  # Edit with your credentials
source .env

# Run the sniper bot locally
python apps/run_sniper.py --coins BTC ETH SOL XRP --timeframe 15m --bankroll 25 --min-edge 0.05 --min-size --max-entry-price 0.20

# Run tests
pytest tests/ -v                        # Run all tests
pytest tests/test_utils.py -v           # Test utility functions
pytest tests/test_bot.py -v             # Test bot module
pytest tests/test_crypto.py -v          # Test encryption
pytest tests/test_signer.py -v          # Test EIP-712 signing

# SSH quick checks
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "tail -50 /var/log/polymarket-sniper.log"
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "cat /opt/polymarket-bot/data/longshot_trades.csv"
```

## Architecture

```
apps/run_sniper.py              # CLI entry point for Longshot/Midshot
strategies/momentum_sniper.py   # Core strategy (SniperConfig, MomentumSniperStrategy)
lib/fair_value.py               # Black-Scholes binary pricing
lib/binance_ws.py               # Binance real-time price feed
lib/trade_logger.py             # CSV trade logging
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
| `bot.py` | Order execution | `TradingBot`, `OrderResult` |
| `fair_value.py` | Fair value calculation | `BinaryFairValue`, `FairValue` |
| `client.py` | API communication | `ClobClient`, `RelayerClient` |
| `signer.py` | EIP-712 signing | `OrderSigner`, `Order` |
| `trade_logger.py` | Trade CSV logging | `TradeLogger` |

### Data Flow
1. Binance WebSocket provides sub-second crypto price updates
2. `BinaryFairValue.calculate()` computes P(Up) from Black-Scholes
3. Compare fair value to Polymarket best ask prices
4. When ask is significantly below fair value (edge > min_edge) → BUY
5. Hold to settlement: binary pays $1 on win, $0 on loss
6. Auto-redeem winnings and compound into next market

## Key Patterns

- **Async methods**: All trading operations are async
- **Config precedence**: Environment vars > YAML file > defaults
- **Builder HMAC auth**: Timestamp + method + path + body signed with api_secret
- **Signature type 2**: Gnosis Safe signatures for Polymarket
- **min_size_mode**: When True, always buys exactly 5 tokens (data collection mode)

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
- Polymarket uses Chainlink BTC/USD for settlement

**Important**: The `docs/` directory contains official Polymarket documentation. When implementing or debugging API features, always reference:
- `docs/developers/CLOB/` - CLOB API endpoints, authentication, orders
- `docs/developers/builders/` - Builder Program, Relayer, gasless transactions
- `docs/api-reference/` - REST API endpoint specifications

## Retired Strategies
- **Contrarian** (`strategies/contrarian.py`): Bought cheap side ($0.03-$0.07) of BTC 5m markets. RETIRED.
- **Flash Crash** (`strategies/flash_crash.py`): Volatility-based probability drop trading. UNUSED.
- **Market Maker** (`strategies/market_maker.py`): Two-sided quoting. Running in observe-only mode, separate from Longshot/Midshot.
