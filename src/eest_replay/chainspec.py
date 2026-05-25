"""Generate a reth/geth-compatible genesis.json from a BlockchainEngineFixture."""

from __future__ import annotations

from typing import Any, Dict

from execution_testing.base_types import Account
from execution_testing.fixtures.blockchain import BlockchainEngineFixture


# Pre-merge forks: activate at block 0.
PRE_MERGE_BLOCK_FORKS = (
    "homesteadBlock",
    "eip150Block",
    "eip155Block",
    "eip158Block",
    "byzantiumBlock",
    "constantinopleBlock",
    "petersburgBlock",
    "istanbulBlock",
    "muirGlacierBlock",
    "berlinBlock",
    "londonBlock",
    "arrowGlacierBlock",
    "grayGlacierBlock",
    "mergeNetsplitBlock",
)

# Timestamp-activated forks in order. We only enable the prefix up to the
# fixture's target fork — turning on Amsterdam for a Prague fixture causes
# geth to expect BAL header fields that the Prague genesis doesn't supply.
TIMESTAMP_FORKS = (
    ("shanghaiTime", "Shanghai"),
    ("cancunTime", "Cancun"),
    ("pragueTime", "Prague"),
    ("osakaTime", "Osaka"),
    ("bpo1Time", "BPO1"),
    ("bpo2Time", "BPO2"),
    ("amsterdamTime", "Amsterdam"),
)


def _fork_name(fixture: BlockchainEngineFixture) -> str:
    fork = fixture.fork
    return fork.name() if hasattr(fork, "name") else str(fork)


def build_genesis(fixture: BlockchainEngineFixture) -> Dict[str, Any]:
    """Return a dict ready to be written as genesis.json for reth or geth."""
    fx_config = fixture.config
    chain_id = int(fx_config.chain_id)
    target_fork = _fork_name(fixture)

    config: Dict[str, Any] = {
        "chainId": chain_id,
        "terminalTotalDifficulty": 0,
        "terminalTotalDifficultyPassed": True,
        "depositContractAddress": "0x00000000219ab540356cbb839cbe05303d7705fa",
    }
    for fork in PRE_MERGE_BLOCK_FORKS:
        config[fork] = 0
    for time_key, fork_name in TIMESTAMP_FORKS:
        config[time_key] = 0
        if fork_name == target_fork:
            break

    if fx_config.blob_schedule is not None:
        config["blobSchedule"] = {
            fork.lower(): {
                "target": int(schedule.target_blobs_per_block),
                "max": int(schedule.max_blobs_per_block),
                "baseFeeUpdateFraction": int(schedule.base_fee_update_fraction),
            }
            for fork, schedule in fx_config.blob_schedule.root.items()
        }

    alloc: Dict[str, Any] = {}
    for address, account in fixture.pre.root.items():
        if account is None:
            continue
        alloc[_addr(address)] = _alloc_entry(account)

    header = fixture.genesis
    genesis: Dict[str, Any] = {
        "config": config,
        "nonce": _hex(header.nonce),
        "timestamp": _hex(header.timestamp),
        "extraData": _hex(header.extra_data) if header.extra_data else "0x",
        "gasLimit": _hex(header.gas_limit),
        "difficulty": _hex(header.difficulty),
        "mixHash": _hex(header.prev_randao),
        "coinbase": _hex(header.fee_recipient),
        "alloc": alloc,
    }
    if header.base_fee_per_gas is not None:
        genesis["baseFeePerGas"] = _hex(header.base_fee_per_gas)
    if header.blob_gas_used is not None:
        genesis["blobGasUsed"] = _hex(header.blob_gas_used)
    if header.excess_blob_gas is not None:
        genesis["excessBlobGas"] = _hex(header.excess_blob_gas)
    # Amsterdam adds slot_number and block_access_list_hash to the header,
    # and geth bal-devnet refuses to re-decode its own genesis without them.
    if header.parent_beacon_block_root is not None:
        genesis["parentBeaconBlockRoot"] = _hex(header.parent_beacon_block_root)
    if header.withdrawals_root is not None:
        genesis["withdrawalsRoot"] = _hex(header.withdrawals_root)
    if header.requests_hash is not None:
        genesis["requestsHash"] = _hex(header.requests_hash)
    if header.block_access_list_hash is not None:
        genesis["blockAccessListHash"] = _hex(header.block_access_list_hash)
    if header.slot_number is not None:
        genesis["slotNumber"] = _hex(header.slot_number)

    return genesis


def _alloc_entry(account: Account) -> Dict[str, Any]:
    out: Dict[str, Any] = {"balance": _hex(account.balance)}
    if int(account.nonce):
        out["nonce"] = _hex(account.nonce)
    if account.code:
        out["code"] = _hex(account.code)
    if account.storage.root:
        out["storage"] = {
            _hex(k): _hex(v) for k, v in account.storage.root.items()
        }
    return out


def _hex(value: Any) -> str:
    """Render a value as a 0x-prefixed hex string."""
    if value is None:
        return "0x0"
    if isinstance(value, int):
        return hex(value)
    s = str(value)
    return s if s.startswith("0x") else f"0x{s}"


def _addr(value: Any) -> str:
    s = str(value)
    return s if s.startswith("0x") else f"0x{s}"
