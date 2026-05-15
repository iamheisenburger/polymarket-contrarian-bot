//! CLOB error handling.
//!
//! Catalog of documented order rejection codes from `docs/developers/CLOB/orders/create-order.md`.
//! We branch on these codes rather than treating every rejection identically —
//! e.g. `MARKET_NOT_READY` is a retry; `INVALID_ORDER_MIN_SIZE` is a config error.

use thiserror::Error;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClobErrorCode {
    /// Price didn't match the market's current tick size.
    InvalidOrderMinTickSize,
    /// Order size below per-market minimum (typically 5 tokens / $1 notional).
    InvalidOrderMinSize,
    /// Identical order has already been placed (same salt/nonce/hash).
    InvalidOrderDuplicated,
    /// Wallet doesn't have funds/allowance.
    InvalidOrderNotEnoughBalance,
    /// GTD expiration too soon (must be ≥ now + 60s).
    InvalidOrderExpiration,
    /// Generic "could not insert order" — usually transient.
    InvalidOrderError,
    /// Matching engine couldn't run — transient.
    ExecutionError,
    /// Marketable order delayed due to PM's `seconds_delay` setting.
    OrderDelayed,
    /// Internal error delaying the order.
    DelayingOrderError,
    /// FOK order couldn't be fully filled and was killed.
    FokOrderNotFilled,
    /// Market not yet accepting orders (e.g. between cycles).
    MarketNotReady,
    /// Anything else we don't recognize.
    Unknown(String),
}

impl ClobErrorCode {
    /// Parse PM's `errorMsg` string into a code. PM doesn't always send a
    /// structured code, so we sniff substrings.
    pub fn parse(error_msg: &str) -> Self {
        let lower = error_msg.to_ascii_lowercase();
        if lower.contains("minimum tick size") {
            Self::InvalidOrderMinTickSize
        } else if lower.contains("lower than the minimum") || lower.contains("size lower") {
            Self::InvalidOrderMinSize
        } else if lower.contains("duplicated") {
            Self::InvalidOrderDuplicated
        } else if lower.contains("not enough balance") || lower.contains("allowance") {
            Self::InvalidOrderNotEnoughBalance
        } else if lower.contains("expiration") {
            Self::InvalidOrderExpiration
        } else if lower.contains("could not insert") {
            Self::InvalidOrderError
        } else if lower.contains("execution") {
            Self::ExecutionError
        } else if lower.contains("delay") {
            // covers both "match delayed" and "error delaying"
            Self::OrderDelayed
        } else if lower.contains("fok") {
            Self::FokOrderNotFilled
        } else if lower.contains("not yet ready") || lower.contains("market not ready") {
            Self::MarketNotReady
        } else {
            Self::Unknown(error_msg.to_string())
        }
    }

    /// Should the caller retry this error after a short delay?
    pub fn is_transient(&self) -> bool {
        matches!(
            self,
            Self::ExecutionError | Self::OrderDelayed | Self::MarketNotReady
        )
    }

    /// Should the caller treat this as a no-op (the order is effectively placed)?
    pub fn is_already_placed(&self) -> bool {
        matches!(self, Self::InvalidOrderDuplicated)
    }
}

#[derive(Debug, Error)]
pub enum ClobError {
    #[error("rejected: {0:?}: {1}")]
    Rejected(ClobErrorCode, String),
    #[error("transport error: {0}")]
    Transport(#[from] reqwest::Error),
    #[error("auth error: {0}")]
    Auth(String),
    #[error("parse error: {0}")]
    Parse(String),
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("{0}")]
    Other(String),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_min_size_error() {
        let s = "order 0xabc is invalid. Size (3) lower than the minimum: 5";
        assert_eq!(ClobErrorCode::parse(s), ClobErrorCode::InvalidOrderMinSize);
    }

    #[test]
    fn parse_min_tick_error() {
        let s = "order is invalid. Price breaks minimum tick size rules";
        assert_eq!(ClobErrorCode::parse(s), ClobErrorCode::InvalidOrderMinTickSize);
    }

    #[test]
    fn parse_duplicate() {
        let s = "order is invalid. Duplicated. Same order ...";
        let c = ClobErrorCode::parse(s);
        assert_eq!(c, ClobErrorCode::InvalidOrderDuplicated);
        assert!(c.is_already_placed());
    }

    #[test]
    fn parse_balance() {
        let s = "not enough balance / allowance";
        assert_eq!(
            ClobErrorCode::parse(s),
            ClobErrorCode::InvalidOrderNotEnoughBalance
        );
    }

    #[test]
    fn parse_market_not_ready_transient() {
        let s = "the market is not yet ready to process new orders";
        let c = ClobErrorCode::parse(s);
        assert_eq!(c, ClobErrorCode::MarketNotReady);
        assert!(c.is_transient());
    }

    #[test]
    fn parse_order_delayed_transient() {
        let s = "order match delayed due to market conditions";
        let c = ClobErrorCode::parse(s);
        assert_eq!(c, ClobErrorCode::OrderDelayed);
        assert!(c.is_transient());
    }

    #[test]
    fn parse_unknown() {
        let s = "some unhandled future error";
        match ClobErrorCode::parse(s) {
            ClobErrorCode::Unknown(m) => assert_eq!(m, s),
            _ => panic!("expected Unknown"),
        }
    }
}
