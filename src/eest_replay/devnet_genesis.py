"""Generate a fresh devnet genesis.json (not derived from a fixture).

Used by the `export` flow: we boot a throwaway geth from this genesis, run a
spec test against it via EEST `execute`, and capture the signed transactions.
The same genesis is emitted alongside the CSV so a downstream consumer
(buildoor) can boot a compatible EL and replay the transactions.
"""

from __future__ import annotations

from typing import Any, Dict

from .chainspec import PRE_MERGE_BLOCK_FORKS, TIMESTAMP_FORKS

# 1,000,000 ETH, enough to fund the EEST seed account for any test run.
DEFAULT_PREFUND_WEI = 10**24

# Standard blob-schedule parameters per fork (mainnet values).
_BLOB_SCHEDULE = {
    "cancun": {"target": 3, "max": 6, "baseFeeUpdateFraction": 3338477},
    "prague": {"target": 6, "max": 9, "baseFeeUpdateFraction": 5007716},
    "osaka": {"target": 6, "max": 9, "baseFeeUpdateFraction": 5007716},
    "bpo1": {"target": 10, "max": 15, "baseFeeUpdateFraction": 8346193},
    "bpo2": {"target": 14, "max": 21, "baseFeeUpdateFraction": 11684671},
    "amsterdam": {"target": 14, "max": 21, "baseFeeUpdateFraction": 11684671},
}

# Empty-state header constants (same across any empty genesis).
_EMPTY_WITHDRAWALS_ROOT = (
    "0x56e81f171bcc55a6ff8345e692c0f86e5b48e01b996cadc001622fb5e363b421"
)
_EMPTY_REQUESTS_HASH = (
    "0xe3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)
_EMPTY_BAL_HASH = (
    "0x1dcc4de8dec75d7aab85b567b6ccd41ad312451b948a7413f0a142fd40d49347"
)
_ZERO_HASH = "0x" + "00" * 32


def _fork_index(fork: str) -> int:
    """Return the position of ``fork`` in the timestamp-fork order, or -1."""
    for i, (_, name) in enumerate(TIMESTAMP_FORKS):
        if name == fork:
            return i
    return -1


def build_devnet_genesis(
    fork: str,
    chain_id: int,
    prefunded: Dict[str, int],
    gas_limit: int = 0x17D7840,
) -> Dict[str, Any]:
    """
    Build a genesis dict activating all forks up to and including ``fork``.

    ``prefunded`` maps 0x-addresses to wei balances (the EEST seed account
    must be included so it can fund the test).
    """
    config: Dict[str, Any] = {
        "chainId": chain_id,
        "terminalTotalDifficulty": 0,
        "terminalTotalDifficultyPassed": True,
        "depositContractAddress": "0x00000000219ab540356cbb839cbe05303d7705fa",
    }
    for block_fork in PRE_MERGE_BLOCK_FORKS:
        config[block_fork] = 0

    target_idx = _fork_index(fork)
    if target_idx < 0:
        raise ValueError(
            f"unknown or pre-merge fork {fork!r}; expected one of "
            f"{[n for _, n in TIMESTAMP_FORKS]}"
        )

    blob_schedule: Dict[str, Any] = {}
    for i, (time_key, name) in enumerate(TIMESTAMP_FORKS):
        if i > target_idx:
            break
        config[time_key] = 0
        if name.lower() in _BLOB_SCHEDULE:
            blob_schedule[name.lower()] = _BLOB_SCHEDULE[name.lower()]
    if blob_schedule:
        config["blobSchedule"] = blob_schedule

    genesis: Dict[str, Any] = {
        "config": config,
        "nonce": "0x0",
        "timestamp": "0x0",
        "extraData": "0x",
        "gasLimit": hex(gas_limit),
        "difficulty": "0x0",
        "mixHash": _ZERO_HASH,
        "coinbase": "0x0000000000000000000000000000000000000000",
        "alloc": {
            _norm(addr): {"balance": hex(bal)}
            for addr, bal in prefunded.items()
        },
        "baseFeePerGas": "0x7",
        "blobGasUsed": "0x0",
        "excessBlobGas": "0x0",
        "parentBeaconBlockRoot": _ZERO_HASH,
    }

    # Amsterdam (EIP-7928 / EIP-7732) adds BAL hash + slot number to the
    # header; geth refuses to re-decode its own genesis without them.
    amsterdam_idx = _fork_index("Amsterdam")
    if amsterdam_idx >= 0 and target_idx >= amsterdam_idx:
        genesis["withdrawalsRoot"] = _EMPTY_WITHDRAWALS_ROOT
        genesis["requestsHash"] = _EMPTY_REQUESTS_HASH
        genesis["blockAccessListHash"] = _EMPTY_BAL_HASH
        genesis["slotNumber"] = "0x0"

    return genesis


def _norm(addr: str) -> str:
    return addr if addr.startswith("0x") else f"0x{addr}"
