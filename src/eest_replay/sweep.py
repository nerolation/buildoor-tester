"""On-chain helpers: balances, sweeping accounts, and recovering test EOAs.

Funding the per-test EOAs spends seed ETH; on a real network that ETH is
worth reclaiming. EEST derives those EOAs deterministically from the
``--eoa-start`` value (key = eoa_start + i), so recording eoa_start is enough
to re-derive and sweep them back later — this module does the deriving +
sweeping.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, List

from execution_testing import EOA, Transaction

from .rpc_proxy import DEFAULT_USER_AGENT, split_basic_auth

GAS_TRANSFER = 21000


def rpc_call(rpc_url: str, method: str, params: list) -> Any:
    """JSON-RPC call honoring inline basic-auth + a WAF-friendly User-Agent."""
    clean, auth = split_basic_auth(rpc_url)
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        clean,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
            **auth,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_balance(rpc_url: str, address: str) -> int:
    """Return the wei balance of ``address`` at latest."""
    return int(rpc_call(rpc_url, "eth_getBalance", [address, "latest"])["result"], 16)


def _nonce(rpc_url: str, address: str) -> int:
    return int(
        rpc_call(rpc_url, "eth_getTransactionCount", [address, "latest"])["result"], 16
    )


def sweep_account(
    rpc_url: str,
    priv_key: int,
    to_addr: str,
    chain_id: int,
    *,
    max_fee_per_gas: int = 5_000_000_000,
    max_priority_fee_per_gas: int = 1_000_000_000,
    min_sweep_wei: int = 0,
) -> tuple[str, int] | None:
    """
    Sweep an account's full balance (minus a gas reserve) to ``to_addr``.

    Returns ``(tx_hash, swept_wei)`` or ``None`` if the balance is too low to
    cover the gas reserve (or below ``min_sweep_wei``).
    """
    acct = EOA(key=priv_key)
    addr = str(acct)
    balance = get_balance(rpc_url, addr)
    reserve = GAS_TRANSFER * max_fee_per_gas
    if balance <= reserve or (balance - reserve) < min_sweep_wei:
        return None
    value = balance - reserve
    tx = Transaction(
        ty=2,
        chain_id=chain_id,
        nonce=_nonce(rpc_url, addr),
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
        gas_limit=GAS_TRANSFER,
        to=to_addr,
        value=value,
        sender=acct,
    ).with_signature_and_sender()
    raw = "0x" + tx.rlp().hex().removeprefix("0x")
    resp = rpc_call(rpc_url, "eth_sendRawTransaction", [raw])
    if "result" not in resp:
        raise RuntimeError(f"sweep of {addr} rejected: {resp.get('error')}")
    return resp["result"], value


@dataclass
class RecoveredAccount:
    """One swept test EOA."""

    address: str
    swept_wei: int
    tx_hash: str


def recover_funded_eoas(
    rpc_url: str,
    eoa_start: int,
    to_addr: str,
    chain_id: int,
    *,
    count: int = 64,
    max_fee_per_gas: int = 5_000_000_000,
    max_priority_fee_per_gas: int = 1_000_000_000,
    wait: bool = True,
) -> List[RecoveredAccount]:
    """
    Re-derive the EOAs EEST funded from ``eoa_start`` and sweep their balances.

    EEST uses ``key = eoa_start + i`` for the i-th allocated EOA, so we scan
    ``count`` keys from ``eoa_start`` and sweep any that hold a balance.
    """
    recovered: List[RecoveredAccount] = []
    for i in range(count):
        result = sweep_account(
            rpc_url, eoa_start + i, to_addr, chain_id,
            max_fee_per_gas=max_fee_per_gas,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
        )
        if result is not None:
            tx_hash, swept = result
            recovered.append(
                RecoveredAccount(str(EOA(key=eoa_start + i)), swept, tx_hash)
            )
    if wait and recovered:
        deadline = time.time() + 180
        pending = {r.tx_hash for r in recovered}
        while pending and time.time() < deadline:
            pending = {
                h for h in pending
                if rpc_call(rpc_url, "eth_getTransactionReceipt", [h]).get("result")
                is None
            }
            if pending:
                time.sleep(4)
    return recovered
