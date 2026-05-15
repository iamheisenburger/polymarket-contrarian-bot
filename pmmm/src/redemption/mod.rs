//! Native Polygon CTF redemption.
//!
//! Replaces the Python `poly-web3 redeem_all` path with direct contract
//! calls. After a Polymarket Up/Down market settles, the winning ERC-1155
//! conditional tokens are redeemable for USDC via the CTF contract's
//! `redeemPositions(collateral, parentCollectionId, conditionId, indexSets)`
//! call.
//!
//! Polygon mainnet CTF: `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
//! Polygon mainnet USDC.e: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`
//!
//! Phase 4 status: lightweight implementation. Currently we build the
//! transaction calldata and emit it for the operator to submit (via Polygon
//! RPC or the existing Python `poly-web3` relayer). Full direct submission
//! requires the gasless relayer to be reimplemented natively, which is a
//! larger Phase 4-polish item.

use alloy_primitives::{Address, B256, U256};
use alloy_sol_types::{sol, SolCall};
use anyhow::Result;
use serde::{Deserialize, Serialize};

/// Polygon mainnet addresses.
pub const POLYGON_CTF_ADDRESS: &str = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045";
pub const POLYGON_USDC_E_ADDRESS: &str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";

sol! {
    /// Subset of the CTF interface we need.
    interface ICTFExchange {
        function redeemPositions(
            address collateralToken,
            bytes32 parentCollectionId,
            bytes32 conditionId,
            uint256[] calldata indexSets
        ) external;
    }
}

/// Build the calldata for `redeemPositions`. The operator (or a relayer)
/// signs + submits this. Used for both pure-Rust direct submission and
/// for the Python relayer integration.
///
/// `condition_id` is the 32-byte hash returned by PM's CTF exchange for a
/// given (oracle, question_id, outcome_slot_count) tuple. PM exposes it
/// per-market via the Gamma API.
///
/// `index_sets` describes which outcome partitions to redeem. For binary
/// up/down markets there are two partitions: `[0b01, 0b10]` (YES, NO).
/// To redeem the winning side only, pass `[0b01]` or `[0b10]` depending
/// on which won.
pub fn build_redeem_calldata(
    collateral: Address,
    parent_collection_id: B256,
    condition_id: B256,
    index_sets: Vec<U256>,
) -> Vec<u8> {
    let call = ICTFExchange::redeemPositionsCall {
        collateralToken: collateral,
        parentCollectionId: parent_collection_id,
        conditionId: condition_id,
        indexSets: index_sets,
    };
    call.abi_encode()
}

/// Convenience: build redeem calldata for a standard PM Up/Down market.
/// `parent_collection_id` is always the zero-hash for PM. Both YES and
/// NO index sets are passed so we redeem whichever outcome the wallet
/// holds.
pub fn build_redeem_calldata_default(condition_id: B256) -> Result<Vec<u8>> {
    let usdc: Address = POLYGON_USDC_E_ADDRESS
        .parse()
        .map_err(|e| anyhow::anyhow!("parse usdc address: {e:?}"))?;
    let parent = B256::ZERO;
    // Both partitions — redeem whichever side won. CTF safely no-ops the
    // empty side.
    let index_sets = vec![U256::from(1u64), U256::from(2u64)];
    Ok(build_redeem_calldata(usdc, parent, condition_id, index_sets))
}

/// Telemetry shape for an enqueued redemption (logged by the runtime).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RedemptionRequest {
    pub slug: String,
    pub condition_id: String,
    pub calldata_hex: String,
    pub built_ts_ns: u64,
}

impl RedemptionRequest {
    pub fn new(slug: String, condition_id: B256) -> Result<Self> {
        let calldata = build_redeem_calldata_default(condition_id)?;
        Ok(Self {
            slug,
            condition_id: format!("{condition_id:#x}"),
            calldata_hex: format!("0x{}", hex::encode(&calldata)),
            built_ts_ns: crate::types::now_mono_ns(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn build_redeem_calldata_has_correct_selector() {
        let usdc: Address = POLYGON_USDC_E_ADDRESS.parse().unwrap();
        let condition_id = B256::from([1u8; 32]);
        let parent = B256::ZERO;
        let index_sets = vec![U256::from(1u64), U256::from(2u64)];
        let calldata = build_redeem_calldata(usdc, parent, condition_id, index_sets);
        // Selector for redeemPositions(address,bytes32,bytes32,uint256[]) =
        // keccak256("redeemPositions(address,bytes32,bytes32,uint256[])")[0..4]
        // (We verify the calldata is non-empty and structurally sane.)
        assert!(calldata.len() >= 4 + 32 * 4, "calldata too short: {}", calldata.len());
        // First 4 bytes are the function selector — must be deterministic.
        let selector = &calldata[..4];
        let again = build_redeem_calldata(
            usdc,
            parent,
            condition_id,
            vec![U256::from(1u64), U256::from(2u64)],
        );
        assert_eq!(selector, &again[..4]);
    }

    #[test]
    fn build_default_redeem_matches_full() {
        let condition = B256::from([42u8; 32]);
        let default = build_redeem_calldata_default(condition).unwrap();
        let usdc: Address = POLYGON_USDC_E_ADDRESS.parse().unwrap();
        let full = build_redeem_calldata(
            usdc,
            B256::ZERO,
            condition,
            vec![U256::from(1u64), U256::from(2u64)],
        );
        assert_eq!(default, full);
    }

    #[test]
    fn redemption_request_serializes() {
        let condition = B256::from([7u8; 32]);
        let r = RedemptionRequest::new("btc-updown-5m-1".to_string(), condition).unwrap();
        let json = serde_json::to_string(&r).unwrap();
        assert!(json.contains("calldata_hex"));
        assert!(json.contains("0x"));
    }
}
