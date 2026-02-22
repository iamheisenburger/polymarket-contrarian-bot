"""
Polymarket Data API wrapper for leaderboard, activity, and positions.

Endpoints:
  - GET /v1/leaderboard  — top traders by category/time period
  - GET /activity         — on-chain activity for a user
  - GET /positions        — current open positions for a user
  - GET /trades           — trades for a user or markets
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"

# Valid categories for leaderboard
CATEGORIES = [
    "OVERALL", "POLITICS", "SPORTS", "CRYPTO", "CULTURE",
    "MENTIONS", "WEATHER", "ECONOMICS", "TECH", "FINANCE",
]

# Categories we care about for copy-trading (skip crypto — proven unprofitable)
COPY_CATEGORIES = ["SPORTS", "POLITICS", "ECONOMICS", "CULTURE", "TECH", "FINANCE"]


@dataclass
class LeaderboardEntry:
    rank: int
    address: str
    username: str
    pnl: float
    volume: float
    category: str = ""
    time_period: str = ""


@dataclass
class TradeActivity:
    wallet: str
    timestamp: int
    condition_id: str
    trade_type: str        # TRADE, REDEEM, etc.
    side: str              # BUY or SELL
    size: float            # tokens
    usdc_size: float       # dollars
    price: float
    asset: str             # token_id for order execution
    outcome: str           # "Yes" or "No"
    outcome_index: int
    title: str             # market question
    slug: str              # market slug
    event_slug: str
    tx_hash: str


@dataclass
class Position:
    wallet: str
    asset: str             # token_id
    condition_id: str
    size: float
    avg_price: float
    initial_value: float
    current_value: float
    cash_pnl: float
    percent_pnl: float
    realized_pnl: float
    cur_price: float
    title: str
    slug: str
    event_slug: str
    outcome: str
    outcome_index: int
    end_date: str
    redeemable: bool


class PolymarketDataAPI:
    """Wrapper for Polymarket Data API (data-api.polymarket.com)."""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> Any:
        """Make GET request and return JSON."""
        url = f"{DATA_API}{path}"
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            if resp.status_code != 200:
                logger.warning(f"API {path} returned {resp.status_code}: {resp.text[:200]}")
                return []
            return resp.json()
        except Exception as e:
            logger.error(f"API error {path}: {e}")
            return []

    # ------------------------------------------------------------------
    # Leaderboard
    # ------------------------------------------------------------------

    def get_leaderboard(
        self,
        category: str = "OVERALL",
        time_period: str = "WEEK",
        order_by: str = "PNL",
        limit: int = 20,
        offset: int = 0,
    ) -> List[LeaderboardEntry]:
        """Get top traders by category and time period."""
        data = self._get("/v1/leaderboard", {
            "category": category,
            "timePeriod": time_period,
            "orderBy": order_by,
            "limit": limit,
            "offset": offset,
        })

        entries = []
        for item in data:
            entries.append(LeaderboardEntry(
                rank=int(item.get("rank", 0)),
                address=item.get("proxyWallet", ""),
                username=item.get("userName", ""),
                pnl=float(item.get("pnl", 0)),
                volume=float(item.get("vol", 0)),
                category=category,
                time_period=time_period,
            ))
        return entries

    def get_top_traders_all_categories(
        self,
        time_period: str = "WEEK",
        limit_per_category: int = 10,
    ) -> Dict[str, List[LeaderboardEntry]]:
        """Get top traders across all relevant categories."""
        results = {}
        for cat in COPY_CATEGORIES:
            entries = self.get_leaderboard(
                category=cat,
                time_period=time_period,
                limit=limit_per_category,
            )
            results[cat] = entries
            time.sleep(0.2)  # Rate limiting
        return results

    # ------------------------------------------------------------------
    # Activity (watch trades)
    # ------------------------------------------------------------------

    def get_activity(
        self,
        user: str,
        trade_type: str = "TRADE",
        side: Optional[str] = None,
        start: Optional[int] = None,
        limit: int = 50,
    ) -> List[TradeActivity]:
        """Get recent activity for a wallet."""
        params: Dict[str, Any] = {
            "user": user,
            "type": trade_type,
            "limit": limit,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
        if side:
            params["side"] = side
        if start:
            params["start"] = start

        data = self._get("/activity", params)

        activities = []
        for item in data:
            activities.append(TradeActivity(
                wallet=item.get("proxyWallet", ""),
                timestamp=int(item.get("timestamp", 0)),
                condition_id=item.get("conditionId", ""),
                trade_type=item.get("type", ""),
                side=item.get("side", ""),
                size=float(item.get("size", 0)),
                usdc_size=float(item.get("usdcSize", 0)),
                price=float(item.get("price", 0)),
                asset=item.get("asset", ""),
                outcome=item.get("outcome", ""),
                outcome_index=int(item.get("outcomeIndex", 0)),
                title=item.get("title", ""),
                slug=item.get("slug", ""),
                event_slug=item.get("eventSlug", ""),
                tx_hash=item.get("transactionHash", ""),
            ))
        return activities

    def get_buys(
        self,
        user: str,
        since_timestamp: Optional[int] = None,
        limit: int = 20,
    ) -> List[TradeActivity]:
        """Get recent BUY trades for a wallet (most common use case)."""
        return self.get_activity(
            user=user,
            trade_type="TRADE",
            side="BUY",
            start=since_timestamp,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(
        self,
        user: str,
        limit: int = 100,
        sort_by: str = "CURRENT",
    ) -> List[Position]:
        """Get current open positions for a wallet."""
        data = self._get("/positions", {
            "user": user,
            "limit": limit,
            "sortBy": sort_by,
            "sortDirection": "DESC",
            "sizeThreshold": 1,
        })

        positions = []
        for item in data:
            positions.append(Position(
                wallet=item.get("proxyWallet", ""),
                asset=item.get("asset", ""),
                condition_id=item.get("conditionId", ""),
                size=float(item.get("size", 0)),
                avg_price=float(item.get("avgPrice", 0)),
                initial_value=float(item.get("initialValue", 0)),
                current_value=float(item.get("currentValue", 0)),
                cash_pnl=float(item.get("cashPnl", 0)),
                percent_pnl=float(item.get("percentPnl", 0)),
                realized_pnl=float(item.get("realizedPnl", 0)),
                cur_price=float(item.get("curPrice", 0)),
                title=item.get("title", ""),
                slug=item.get("slug", ""),
                event_slug=item.get("eventSlug", ""),
                outcome=item.get("outcome", ""),
                outcome_index=int(item.get("outcomeIndex", 0)),
                end_date=item.get("endDate", ""),
                redeemable=bool(item.get("redeemable", False)),
            ))
        return positions
