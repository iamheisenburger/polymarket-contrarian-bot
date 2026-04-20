#!/usr/bin/env python3
"""
Measure feed latency leadership empirically.

Runs 5 feeds (Coinbase + Binance + Bybit Spot + Bybit Perps + OKX Spot)
simultaneously for 120s. Kraken dropped (0.5% first-tick share in prior
measurement — dead weight). Records every price tick with (exchange, coin,
price, timestamp_ns). Then computes:

  1. Lead-time per exchange per coin.
     For each price UPDATE on coin C (defined as: new price != previous
     observed price for that coin across ANY exchange), note which
     exchange broadcast it first. Lead time = time between first
     observer and each subsequent observer for that price level.

  2. First-tick-win frequency per exchange per coin.
     What fraction of price updates does each exchange observe FIRST?

  3. Perp vs spot lead.
     For each coin supported by Bybit perps, does the perp tick first
     or after spot? Compares time between perp's first observation of
     a new price and first spot observation.

Outputs: table of lead times + first-win percentages + perp-spot lag.

This tells us:
  - Which exchange is actually fastest (per coin).
  - Whether perps lead or lag spot (confirms/refutes lead-indicator theory).
  - Whether adding more feeds genuinely helps (first-tick-win > 0 for each).
"""
import asyncio
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DURATION_S = 120  # measurement window


async def main():
    from lib.coinbase_ws import CoinbasePriceFeed
    from lib.binance_ws import BinancePriceFeed
    from lib.bybit_spot_ws import BybitSpotPriceFeed
    from lib.bybit_perp_ws import BybitPerpsPriceFeed
    from lib.okx_spot_ws import OKXSpotPriceFeed

    # Coins covered by most exchanges (drop HYPE — only Coinbase has it)
    coins = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]

    feeds = {
        "coinbase":  CoinbasePriceFeed(coins=[c for c in coins if c != "BNB"]),
        "binance":   BinancePriceFeed(coins=coins),
        "bybit_s":   BybitSpotPriceFeed(coins=coins),
        "bybit_p":   BybitPerpsPriceFeed(coins=coins),
        "okx_s":     OKXSpotPriceFeed(coins=coins),
    }

    # ticks[(exchange, coin)] = list of (timestamp_ns, price)
    ticks = defaultdict(list)

    def make_cb(exchange_name):
        def _cb(coin, price):
            ticks[(exchange_name, coin.upper())].append((time.time_ns(), price))
        return _cb

    for ex_name, feed in feeds.items():
        feed.on_price(make_cb(ex_name))

    print(f"Starting {len(feeds)} feeds for {DURATION_S}s...")
    start_results = await asyncio.gather(
        *[f.start() for f in feeds.values()], return_exceptions=True
    )
    for (name, _), res in zip(feeds.items(), start_results):
        if isinstance(res, Exception):
            print(f"  {name}: start FAILED: {res}")
        else:
            print(f"  {name}: started OK (connected={feeds[name].connected})")

    print(f"\nCollecting ticks for {DURATION_S}s...")
    await asyncio.sleep(DURATION_S)

    await asyncio.gather(*[f.stop() for f in feeds.values()], return_exceptions=True)

    # ---- Analysis ----
    print("\n" + "=" * 80)
    print("Per-exchange tick counts")
    print("=" * 80)
    print(f"{'exchange':10s} " + " ".join(f"{c:>7s}" for c in coins) + "  " + f"{'total':>7s}")
    for ex in feeds.keys():
        counts = [len(ticks.get((ex, c), [])) for c in coins]
        print(f"{ex:10s} " + " ".join(f"{n:>7d}" for n in counts) + "  " + f"{sum(counts):>7d}")

    # ---- First-tick-win ----
    # For each coin, group updates by "event" = new price level observed.
    # An event = a price appearing in ANY feed. Within each event, the FIRST
    # feed to broadcast that price wins. We require the price change to be
    # a real move (not noise); tolerance ±$1e-8.
    print("\n" + "=" * 80)
    print("First-tick-wins per coin (of N distinct price moves)")
    print("=" * 80)

    for coin in coins:
        # Merge all ticks for this coin into time-sorted stream.
        all_ticks = []
        for ex in feeds.keys():
            for ts_ns, px in ticks.get((ex, coin), []):
                all_ticks.append((ts_ns, ex, px))
        all_ticks.sort()

        if not all_ticks:
            print(f"\n{coin}: no ticks from any exchange.")
            continue

        # Walk through and detect "first observer" of each new price.
        # A "new price" = price different from the last price broadcast by the
        # first observer. We use a rolling 500ms de-dup window so multiple
        # exchanges echoing the same tick get grouped.
        wins = defaultdict(int)
        total_events = 0
        lead_times_per_exchange = defaultdict(list)  # ex -> list of ms behind the leader

        last_event_ts = 0
        last_event_price = None
        last_event_seen = set()

        for ts_ns, ex, px in all_ticks:
            ts_ms = ts_ns / 1_000_000
            # New event if: no current event, or >500ms since last, or price differs by > 1 bps
            is_new_event = (
                last_event_price is None
                or (ts_ms - last_event_ts) > 500
                or (last_event_price > 0 and abs(px - last_event_price) / last_event_price > 0.0001)
            )
            if is_new_event:
                # Previous event closed. Start new.
                total_events += 1
                wins[ex] += 1
                last_event_ts = ts_ms
                last_event_price = px
                last_event_seen = {ex}
            else:
                # Same event — this feed is echoing. Record its lag vs leader.
                if ex not in last_event_seen:
                    lag_ms = ts_ms - last_event_ts
                    lead_times_per_exchange[ex].append(lag_ms)
                    last_event_seen.add(ex)

        print(f"\n{coin}: {total_events} distinct price events")
        for ex in feeds.keys():
            w = wins.get(ex, 0)
            pct = (w / total_events * 100) if total_events else 0
            lags = lead_times_per_exchange.get(ex, [])
            if lags:
                med_lag = sorted(lags)[len(lags) // 2]
                lag_str = f"median lag when NOT first: {med_lag:>6.1f}ms"
            else:
                lag_str = ""
            print(f"  {ex:10s} first_wins={w:>4d} ({pct:>4.1f}%)  {lag_str}")

    # ---- Perp vs spot ----
    print("\n" + "=" * 80)
    print("Perp vs spot lead: for each coin, does bybit_p lead or lag spot-agg?")
    print("=" * 80)
    for coin in coins:
        # Build spot-agg stream = min-timestamp across spot feeds per price
        spot_exchanges = ["coinbase", "binance", "kraken", "bybit_s"]
        spot_ticks = []
        for ex in spot_exchanges:
            for ts_ns, px in ticks.get((ex, coin), []):
                spot_ticks.append((ts_ns, px))
        spot_ticks.sort()

        perp_ticks = ticks.get(("bybit_p", coin), [])

        if not spot_ticks or not perp_ticks:
            print(f"\n{coin}: insufficient data (spot={len(spot_ticks)}, perp={len(perp_ticks)})")
            continue

        # For each perp tick, find the first spot tick that has a price
        # within 10bps of the perp price (accounting for basis). Compare
        # timestamps — negative = perp leads, positive = spot leads.
        leads = []
        spot_idx = 0
        for p_ts, p_px in perp_ticks:
            # advance spot_idx to find closest-price spot tick in +/- 2s window
            while spot_idx < len(spot_ticks) and spot_ticks[spot_idx][0] < p_ts - 2_000_000_000:
                spot_idx += 1
            # Look at spot ticks within 2s window for a matching price
            best_dt = None
            for j in range(max(0, spot_idx - 20), min(len(spot_ticks), spot_idx + 100)):
                s_ts, s_px = spot_ticks[j]
                if s_px > 0 and abs(s_px - p_px) / s_px < 0.001:  # same price within 10 bps
                    dt = (s_ts - p_ts) / 1_000_000  # ms; +ve = spot later = perp leads
                    if best_dt is None or abs(dt) < abs(best_dt):
                        best_dt = dt
            if best_dt is not None and abs(best_dt) < 2000:
                leads.append(best_dt)

        if not leads:
            print(f"\n{coin}: no matched perp-spot pairs")
            continue
        leads.sort()
        n = len(leads)
        med = leads[n // 2]
        perp_leads_frac = sum(1 for v in leads if v > 0) / n
        print(f"\n{coin}: {n} matched pairs")
        print(f"  median perp->spot delta: {med:+.1f}ms  ({'perp LEADS' if med > 0 else 'perp LAGS' if med < 0 else 'tied'})")
        print(f"  perp leads in: {perp_leads_frac * 100:.0f}% of cases")


if __name__ == "__main__":
    asyncio.run(main())
