"""
Shadow Maker IC — simulates the full dual-GTD maker bot lifecycle tick-by-tick.

Unlike the basic MakerCollector which records "ask dropped to price X",
this simulates the EXACT bot logic: placement timing, momentum gating,
reversal cancellation, dual-side management, and state machine transitions.

Runs N configs simultaneously for offline sweeps. One CSV output with
config_id column to group results.

Trust level: 10/10 (matches live bot logic, conservative on cancel timing).
"""

import csv
import os
import threading
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

DURATION_5M = 300  # 5-minute markets


@dataclass
class ShadowConfig:
    """One sweep configuration to simulate."""
    config_id: str
    rest_price: float = 0.52
    cancel_momentum: float = 0.0008
    place_elapsed: float = 150.0
    max_concurrent: int = 1
    min_tte: float = 30.0
    cancel_tte: float = 20.0
    cancel_latency: float = 3.0  # Simulated CLOB cancel latency in seconds
    # Fill realism (added Apr-2026 after reality showed 50pp WR gap):
    # - Flash touches at rest_price don't fill us (queue). Require ask to drop
    #   BELOW rest_price by at least confirm_depth dollars to count as fill.
    # - Confirm by requiring the dip to persist for confirm_seconds of elapsed
    #   time. Rejects transient ticks that never actually cleared our queue.
    # These two together proxy queue position without needing book-depth data.
    confirm_depth: float = 0.005    # Ask must reach rest_price - confirm_depth
    confirm_seconds: float = 1.5    # For this duration of sustained dip
    # True queue-based fill model (added Apr-2026 v2, overrides confirm_depth
    # when queue_depth_known is True on the order). Queue-ahead is snapshotted
    # from bid depth at placement; on each trade event we decrement. Fill only
    # when queue_ahead ≤ 0 AND ask ≤ rest_price.
    use_queue_model: bool = True
    # Parity with bot: minimum |momentum| required at placement (mirrors bot's
    # min_momentum). Without this, shadow over-places vs live.
    min_momentum: float = 0.0005
    # Parity with bot: max queue ahead to allow placement (0 = disabled).
    max_queue_ahead: float = 0.0
    # Parity with bot: don't place if ask > max_entry_price.
    max_entry_price: float = 0.95


@dataclass
class ShadowOrder:
    """Simulated order state for one side of a dual-GTD pair."""
    slug: str
    coin: str
    config_id: str
    side: str  # "up" or "down"
    rest_price: float
    state: str = "RESTING"  # RESTING / CANCELLING / FILLED / CANCELLED

    placed_at_elapsed: float = 0.0
    placed_at_ts: str = ""
    momentum_at_place: float = 0.0

    filled_at_elapsed: float = 0.0
    momentum_at_fill: float = 0.0

    cancelled_at_elapsed: float = 0.0
    momentum_at_cancel: float = 0.0
    cancel_initiated_elapsed: float = 0.0  # When cancel was triggered (before latency)
    cancel_reason: str = ""

    outcome: str = ""  # "won" / "lost" / "" (pending or cancelled)

    # Fill confirmation tracking (realism improvement):
    # Once ask drops to confirmation threshold, track when this started.
    # Fill is only confirmed if ask stays below threshold for confirm_seconds.
    fill_dip_started_elapsed: float = 0.0  # 0 means not currently in dip

    # Queue model state (v2 realism — requires depth + trade events):
    # At placement, snapshot the total bid size at or above rest_price on our
    # side. That's our queue_ahead. On each incoming trade at our price or
    # better that doesn't hit us, decrement. Fill only when queue_ahead ≤ 0
    # AND a subsequent trade occurs at ≤ rest_price that would consume us.
    queue_ahead: float = 0.0          # tokens ahead of us; 0 means next in line
    queue_depth_known: bool = False   # True if we snapshotted depth at placement
    size_remaining: float = 5.0       # our order size remaining to fill

    # Telemetry (written to CSV at flush for post-hoc analysis):
    ask_at_place: float = 0.0
    bid_at_place: float = 0.0
    queue_ahead_at_place: float = 0.0


# Coin-level state per config per window
COIN_IDLE = "IDLE"
COIN_RESTING = "RESTING"
COIN_FILLED = "FILLED"
COIN_CANCELLED = "CANCELLED"
COIN_DONE = "DONE"

HEADER = [
    'timestamp', 'config_id', 'market_slug', 'coin', 'side', 'rest_price',
    'place_elapsed', 'fill_elapsed', 'cancel_elapsed',
    'momentum_at_place', 'momentum_at_fill', 'momentum_at_cancel',
    'cancel_momentum_threshold', 'was_cancelled', 'cancel_reason',
    'was_filled', 'outcome',
    # Apr 20 2026 v2 extension: queue/book state at placement for post-hoc
    # calibration against live fills. Older rows (v1) will have empty values.
    'ask_at_place', 'bid_at_place', 'queue_ahead_at_place',
]


class ShadowMaker:
    """Shadow dual-GTD maker bot — tick-by-tick simulation."""

    def __init__(self, output_path: str, configs: List[ShadowConfig]):
        self._output = output_path
        self._configs = configs
        self._lock = threading.Lock()

        # Per-config, per-slug, per-coin state: "{config_id}:{slug}:{coin}" -> coin state
        self._coin_state: Dict[str, str] = {}

        # Per-config, per-slug, per-coin, per-side orders: "{config_id}:{slug}:{coin}:{side}" -> ShadowOrder
        self._orders: Dict[str, ShadowOrder] = {}

        # Active count cache per config: "{config_id}" -> count of RESTING/FILLED coins
        self._active_count: Dict[str, int] = {c.config_id: 0 for c in configs}

        # Completed records awaiting flush
        self._pending: List[ShadowOrder] = []

        # Stats
        self._total_fills = 0
        self._total_cancels = 0
        self._total_resolved = 0

        # Write header if new file
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            with open(output_path, 'w', newline='') as f:
                csv.writer(f).writerow(HEADER)

    def _coin_key(self, config_id: str, slug: str, coin: str) -> str:
        return f"{config_id}:{slug}:{coin}"

    def _order_key(self, config_id: str, slug: str, coin: str, side: str) -> str:
        return f"{config_id}:{slug}:{coin}:{side}"

    def _get_coin_state(self, config_id: str, slug: str, coin: str) -> str:
        return self._coin_state.get(self._coin_key(config_id, slug, coin), COIN_IDLE)

    def _set_coin_state(self, config_id: str, slug: str, coin: str, state: str):
        key = self._coin_key(config_id, slug, coin)
        old_state = self._coin_state.get(key, COIN_IDLE)
        self._coin_state[key] = state
        # Maintain active count cache
        was_active = old_state in (COIN_RESTING, COIN_FILLED)
        is_active = state in (COIN_RESTING, COIN_FILLED)
        if was_active and not is_active:
            self._active_count[config_id] = max(0, self._active_count.get(config_id, 0) - 1)
        elif not was_active and is_active:
            self._active_count[config_id] = self._active_count.get(config_id, 0) + 1

    def _count_active(self, config_id: str) -> int:
        """Count coins in RESTING or FILLED state for this config (cached)."""
        return self._active_count.get(config_id, 0)

    def tick(self, coin: str, slug: str, elapsed: float,
             spot: float, strike: float,
             up_ask: float, up_bid: float,
             down_ask: float, down_bid: float,
             up_bid_depth_at: Optional[Dict[float, float]] = None,
             down_bid_depth_at: Optional[Dict[float, float]] = None):
        """
        Called every tick. Simulates the full dual-GTD lifecycle for each config.

        up_bid_depth_at / down_bid_depth_at: optional dict mapping price → total
        bid size at that price. Used to snapshot queue_ahead at placement.
        Passing None disables queue model for this tick (falls back to dip-based
        fill detection).
        """
        if strike <= 0 or spot <= 0:
            return
        if elapsed < 0 or elapsed > DURATION_5M:
            return

        tte = DURATION_5M - elapsed
        disp = (spot - strike) / strike
        now_ts = datetime.now(timezone.utc).isoformat()

        for cfg in self._configs:
            self._tick_config(cfg, coin, slug, elapsed, tte, disp,
                              up_ask, up_bid, down_ask, down_bid, now_ts,
                              up_bid_depth_at, down_bid_depth_at)

    def on_trade(self, slug: str, coin: str, traded_outcome_side: str,
                 trade_price: float, trade_size: float):
        """
        Called when a SELL trade event arrives on this side's token.

        Bid-side queue mechanics (standard FIFO):
          - Sell orders match best bid first (highest price). A SELL trade at
            price P executes at the maker's resting price (=P if the match is
            within that level). So trade.price observed = maker price that was
            hit.
          - Trades at price > our rest_price hit bids AHEAD of us (higher price =
            higher priority). Decrement our queue_ahead by the traded size.
          - Trades at price == our rest_price: FIFO within the level. If queue
            ahead at this level > 0, it gets hit first; else the excess hits us.
          - Trades at price < our rest_price: irrelevant (that level is behind
            us in the queue).

        Float-price tolerance for equality: 0.001 (sub-cent).
        """
        with self._lock:
            for key, order in self._orders.items():
                if order.slug != slug or order.coin != coin:
                    continue
                if order.side != traded_outcome_side:
                    continue
                if order.state != "RESTING" and order.state != "CANCELLING":
                    continue
                if not order.queue_depth_known:
                    continue

                if trade_price > order.rest_price + 0.001:
                    # Hit a higher level; decrement queue_ahead.
                    order.queue_ahead = max(0.0, order.queue_ahead - trade_size)
                elif abs(trade_price - order.rest_price) <= 0.001:
                    # At our level. Queue-ahead at this level eats first, then us.
                    if order.queue_ahead >= trade_size:
                        order.queue_ahead -= trade_size
                    else:
                        hit_size = trade_size - order.queue_ahead
                        order.queue_ahead = 0.0
                        fill_amount = min(hit_size, order.size_remaining)
                        if fill_amount > 0:
                            order.size_remaining -= fill_amount
                            if order.size_remaining <= 0.001:
                                order.state = "FILLED"
                                self._total_fills += 1
                # else trade_price < our rest_price: trade was below us, no effect.

    def _tick_config(self, cfg: ShadowConfig, coin: str, slug: str,
                     elapsed: float, tte: float, disp: float,
                     up_ask: float, up_bid: float,
                     down_ask: float, down_bid: float, now_ts: str,
                     up_bid_depth_at: Optional[Dict[float, float]] = None,
                     down_bid_depth_at: Optional[Dict[float, float]] = None):
        """Simulate one config for one coin for one tick."""

        coin_state = self._get_coin_state(cfg.config_id, slug, coin)

        # Already done for this window
        if coin_state == COIN_DONE:
            return

        # ── Phase 1: Cancel check (RESTING and CANCELLING orders) ──

        if coin_state == COIN_RESTING:
            for side in ["up", "down"]:
                okey = self._order_key(cfg.config_id, slug, coin, side)
                order = self._orders.get(okey)
                if not order:
                    continue

                # Complete pending cancels after latency expires
                if order.state == "CANCELLING":
                    if elapsed >= order.cancel_initiated_elapsed + cfg.cancel_latency:
                        order.state = "CANCELLED"
                        order.cancelled_at_elapsed = elapsed
                        self._total_cancels += 1
                    continue  # Still cancelling or just cancelled — skip to fill check

                if order.state != "RESTING":
                    continue

                should_cancel = False
                cancel_reason = ""

                # Window ending — instant cancel (no latency, market is ending)
                if tte < cfg.cancel_tte:
                    should_cancel = True
                    cancel_reason = "window_ending"
                    # Window ending is instant — no latency simulation
                    order.state = "CANCELLED"
                    order.cancelled_at_elapsed = elapsed
                    order.momentum_at_cancel = disp
                    order.cancel_reason = cancel_reason
                    self._total_cancels += 1
                    continue

                # Momentum reversal — enters CANCELLING state (simulates 3s CLOB latency)
                if side == "up" and disp < cfg.cancel_momentum:
                    should_cancel = True
                    cancel_reason = "momentum_reversed"
                elif side == "down" and disp > -cfg.cancel_momentum:
                    should_cancel = True
                    cancel_reason = "momentum_reversed"

                if should_cancel:
                    order.state = "CANCELLING"
                    order.cancel_initiated_elapsed = elapsed
                    order.momentum_at_cancel = disp
                    order.cancel_reason = cancel_reason
                    # Order is still LIVE on the book during CANCELLING — fills can happen

            # After processing, check if ALL orders are fully cancelled
            all_done = True
            for side in ["up", "down"]:
                okey = self._order_key(cfg.config_id, slug, coin, side)
                order = self._orders.get(okey)
                if order and order.state not in ("CANCELLED", "FILLED"):
                    all_done = False

            if all_done:
                has_fill = any(
                    self._orders.get(self._order_key(cfg.config_id, slug, coin, s))
                    and self._orders[self._order_key(cfg.config_id, slug, coin, s)].state == "FILLED"
                    for s in ["up", "down"]
                )
                if not has_fill:
                    reasons = set()
                    for side in ["up", "down"]:
                        okey = self._order_key(cfg.config_id, slug, coin, side)
                        order = self._orders.get(okey)
                        if order:
                            reasons.add(order.cancel_reason)

                    if "window_ending" in reasons:
                        self._set_coin_state(cfg.config_id, slug, coin, COIN_DONE)
                    else:
                        self._set_coin_state(cfg.config_id, slug, coin, COIN_CANCELLED)
                    self._complete_orders(cfg.config_id, slug, coin)
                    return

        # ── Phase 2: Fill check (RESTING and CANCELLING orders) ──
        # CANCELLING orders are still live on the book — fills CAN happen during
        # the cancel latency window. This is the cancel-race simulation.

        coin_state = self._get_coin_state(cfg.config_id, slug, coin)
        if coin_state == COIN_RESTING:
            fill_threshold = cfg.rest_price - cfg.confirm_depth
            for side in ["up", "down"]:
                okey = self._order_key(cfg.config_id, slug, coin, side)
                order = self._orders.get(okey)
                if not order or order.state not in ("RESTING", "CANCELLING"):
                    continue

                # If queue model already filled via on_trade(), detect here and
                # finalize state (momentum-at-fill + cancel-opposite).
                if order.state == "FILLED":
                    order.filled_at_elapsed = elapsed
                    order.momentum_at_fill = disp
                    opp_side = "down" if side == "up" else "up"
                    opp_key = self._order_key(cfg.config_id, slug, coin, opp_side)
                    opp_order = self._orders.get(opp_key)
                    if opp_order and opp_order.state == "RESTING":
                        opp_order.state = "CANCELLED"
                        opp_order.cancelled_at_elapsed = elapsed
                        opp_order.momentum_at_cancel = disp
                        opp_order.cancel_reason = "opposite_filled"
                        self._total_cancels += 1
                    self._set_coin_state(cfg.config_id, slug, coin, COIN_FILLED)
                    break

                side_ask = up_ask if side == "up" else down_ask
                if side_ask <= 0:
                    order.fill_dip_started_elapsed = 0.0
                    continue

                # Queue-model path: if we have queue depth info for this order,
                # fill only when queue_ahead ≤ 0 AND ask has reached our price.
                # on_trade() handles the actual queue decrement + fill completion.
                # But ALSO trigger fill here if queue is clear and ask is low
                # (handles case where no trade event arrived but ask crossed).
                if cfg.use_queue_model and order.queue_depth_known:
                    if order.queue_ahead <= 0 and side_ask <= cfg.rest_price:
                        # Queue cleared + ask crossed our price → fill.
                        order.state = "FILLED"
                        order.filled_at_elapsed = elapsed
                        order.momentum_at_fill = disp
                        order.size_remaining = 0.0
                        self._total_fills += 1

                        opp_side = "down" if side == "up" else "up"
                        opp_key = self._order_key(cfg.config_id, slug, coin, opp_side)
                        opp_order = self._orders.get(opp_key)
                        if opp_order and opp_order.state == "RESTING":
                            opp_order.state = "CANCELLED"
                            opp_order.cancelled_at_elapsed = elapsed
                            opp_order.momentum_at_cancel = disp
                            opp_order.cancel_reason = "opposite_filled"
                            self._total_cancels += 1
                        self._set_coin_state(cfg.config_id, slug, coin, COIN_FILLED)
                        break
                    # Queue not clear or ask above rest — no fill this tick
                    continue

                # Fallback dwell-based path (when queue depth was unavailable)
                if side_ask <= fill_threshold:
                    if order.fill_dip_started_elapsed <= 0:
                        order.fill_dip_started_elapsed = elapsed
                    dip_duration = elapsed - order.fill_dip_started_elapsed
                    if dip_duration < cfg.confirm_seconds:
                        continue
                    order.state = "FILLED"
                    order.filled_at_elapsed = elapsed
                    order.momentum_at_fill = disp
                    self._total_fills += 1

                    opp_side = "down" if side == "up" else "up"
                    opp_key = self._order_key(cfg.config_id, slug, coin, opp_side)
                    opp_order = self._orders.get(opp_key)
                    if opp_order and opp_order.state == "RESTING":
                        opp_order.state = "CANCELLED"
                        opp_order.cancelled_at_elapsed = elapsed
                        opp_order.momentum_at_cancel = disp
                        opp_order.cancel_reason = "opposite_filled"
                        self._total_cancels += 1

                    self._set_coin_state(cfg.config_id, slug, coin, COIN_FILLED)
                    break
                else:
                    order.fill_dip_started_elapsed = 0.0

        # ── Phase 3: Placement check (IDLE coins) ──

        coin_state = self._get_coin_state(cfg.config_id, slug, coin)
        if coin_state != COIN_IDLE:
            return

        # Elapsed check
        if elapsed < cfg.place_elapsed:
            return

        # TTE check
        if tte < cfg.min_tte:
            return

        # Momentum check — parity with bot's min_momentum placement gate.
        # Shadow must also require |momentum| >= min_momentum to match bot.
        # Also require >= cancel_momentum to avoid instant self-cancel.
        abs_disp = abs(disp)
        placement_min = max(cfg.cancel_momentum, cfg.min_momentum)
        if abs_disp < placement_min:
            return

        # Concurrent check
        if self._count_active(cfg.config_id) >= cfg.max_concurrent:
            return

        # Place both sides (skip sides that would cross the book)
        placed = False
        for side in ["up", "down"]:
            side_ask = up_ask if side == "up" else down_ask
            # Anti-crossing: skip if ask <= rest_price
            if side_ask > 0 and side_ask <= cfg.rest_price:
                continue

            # Parity with bot: skip if ask > max_entry_price.
            if cfg.max_entry_price > 0 and side_ask > 0 and side_ask > cfg.max_entry_price:
                continue

            # Queue snapshot: sum bid sizes at ≥ rest_price on our side.
            # This is our FIFO queue_ahead — tokens ahead of us that must trade
            # before we can fill.
            queue_ahead = 0.0
            queue_known = False
            best_bid = 0.0
            depth_map = up_bid_depth_at if side == "up" else down_bid_depth_at
            if depth_map:
                for p, sz in depth_map.items():
                    if p >= cfg.rest_price:
                        queue_ahead += sz
                    if p > best_bid:
                        best_bid = p
                queue_known = True

            # Parity with bot: queue-aware placement gate.
            if cfg.max_queue_ahead > 0 and queue_known and queue_ahead > cfg.max_queue_ahead:
                continue

            okey = self._order_key(cfg.config_id, slug, coin, side)
            self._orders[okey] = ShadowOrder(
                slug=slug,
                coin=coin,
                config_id=cfg.config_id,
                side=side,
                rest_price=cfg.rest_price,
                state="RESTING",
                placed_at_elapsed=elapsed,
                placed_at_ts=now_ts,
                momentum_at_place=disp,
                queue_ahead=queue_ahead,
                queue_depth_known=queue_known,
                size_remaining=5.0,
                ask_at_place=side_ask,
                bid_at_place=best_bid,
                queue_ahead_at_place=queue_ahead,
            )
            placed = True

        if placed:
            self._set_coin_state(cfg.config_id, slug, coin, COIN_RESTING)

    def _complete_orders(self, config_id: str, slug: str, coin: str):
        """Move completed orders to pending for flush."""
        with self._lock:
            for side in ["up", "down"]:
                okey = self._order_key(config_id, slug, coin, side)
                order = self._orders.pop(okey, None)
                if order:
                    self._pending.append(order)

    def resolve(self, slug: str, winning_side: str):
        """Resolve outcome for all filled orders in this market window."""
        with self._lock:
            # Check orders still in active state
            to_complete = []
            for key, order in list(self._orders.items()):
                if order.slug != slug:
                    continue
                if order.state == "FILLED":
                    order.outcome = "won" if order.side == winning_side else "lost"
                    self._total_resolved += 1
                to_complete.append(key)

            for key in to_complete:
                order = self._orders.pop(key)
                self._pending.append(order)
                # Clean up coin state (and active count)
                ck = self._coin_key(order.config_id, slug, order.coin)
                old_st = self._coin_state.pop(ck, COIN_IDLE)
                if old_st in (COIN_RESTING, COIN_FILLED):
                    self._active_count[order.config_id] = max(0, self._active_count.get(order.config_id, 0) - 1)

            # Also resolve pending records that haven't been resolved yet
            for order in self._pending:
                if order.slug == slug and order.state == "FILLED" and not order.outcome:
                    order.outcome = "won" if order.side == winning_side else "lost"
                    self._total_resolved += 1

    def on_market_change(self, old_slug: str):
        """Clean up state for a market that has ended/changed."""
        # Move any remaining orders for old_slug to pending
        with self._lock:
            to_remove = [key for key in self._orders if f":{old_slug}:" in key]
            for key in to_remove:
                order = self._orders.pop(key)
                self._pending.append(order)

            # Clean up coin state (and active counts)
            to_remove_cs = [key for key in self._coin_state if f":{old_slug}:" in key]
            for key in to_remove_cs:
                old_st = self._coin_state.pop(key)
                if old_st in (COIN_RESTING, COIN_FILLED):
                    # Extract config_id from key "config_id:slug:coin"
                    cfg_id = key.split(":")[0]
                    self._active_count[cfg_id] = max(0, self._active_count.get(cfg_id, 0) - 1)

    def flush(self):
        """Write resolved records to CSV and clear them from pending."""
        with self._lock:
            resolved = [o for o in self._pending if o.outcome or o.state == "CANCELLED"]
            if not resolved:
                return

            try:
                with open(self._output, 'a', newline='') as f:
                    writer = csv.writer(f)
                    for o in resolved:
                        writer.writerow([
                            o.placed_at_ts,
                            o.config_id,
                            o.slug,
                            o.coin,
                            o.side,
                            o.rest_price,
                            round(o.placed_at_elapsed, 1),
                            round(o.filled_at_elapsed, 1) if o.state == "FILLED" else '',
                            round(o.cancelled_at_elapsed, 1) if o.state == "CANCELLED" else '',
                            round(o.momentum_at_place, 8),
                            round(o.momentum_at_fill, 8) if o.state == "FILLED" else '',
                            round(o.momentum_at_cancel, 8) if o.state == "CANCELLED" else '',
                            o.rest_price,  # cancel_momentum_threshold stored in config_id
                            o.state == "CANCELLED",
                            o.cancel_reason,
                            o.state == "FILLED",
                            o.outcome,
                            round(o.ask_at_place, 4),
                            round(o.bid_at_place, 4),
                            round(o.queue_ahead_at_place, 2),
                        ])

                self._pending = [o for o in self._pending if o not in resolved]

            except Exception as e:
                logger.error(f"ShadowMaker flush failed: {e}")

    def get_stats(self) -> str:
        """Return stats string for logging."""
        return (f"ShadowMaker: fills={self._total_fills} "
                f"cancels={self._total_cancels} resolved={self._total_resolved} "
                f"pending={len(self._pending)} configs={len(self._configs)}")
