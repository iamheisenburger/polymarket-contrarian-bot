//! Polymarket CLOB native client.
//!
//! Replaces `py-clob-client`. Direct REST + WS implementation against the
//! documented PM API (`docs/developers/CLOB/`).

pub mod error;
pub mod market_ws;
pub mod rest;
pub mod user_ws;

pub use error::{ClobError, ClobErrorCode};
pub use market_ws::{
    BookSnapshot, LastTradePrice, MarketEvent, MarketWs, OrderbookLevel, PriceChange,
    TickSizeChange, PM_MARKET_WS_URL,
};
pub use rest::{CancelResponse, ClobClient, ClobConfig, OrderResponse};
pub use user_ws::{MakerOrderRef, OrderEvent, TradeEvent, UserEvent, UserWs, PM_USER_WS_URL};
