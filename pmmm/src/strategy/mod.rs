//! Strategy module — theo computation, the `Quoter` trait, and concrete
//! quoter implementations (v7.0 favorite-bias is shipped; spread two-sided
//! is a placeholder for later).

pub mod maker_quoter;
pub mod markout;
pub mod quoter;
pub mod theo;

pub use maker_quoter::{PaamConfig, PaamQuoter};
pub use markout::{schedule_markout, FillSnapshot};
pub use quoter::{QuoteIntent, Quoter, QuoterContext, V7Quoter};
pub use theo::{
    bs_digital_up, brier, logit, realized_vol, sigmoid, theo_p_up_at, TheoV6Params,
    DEFAULT_HORIZONS_S,
};
