"""Turn spec tests into real transactions and get them onto a chain.

Two entry points, both built on EEST `execute remote` (which funds a sender,
deploys the deterministic factory, deploys the test's contracts, and sends the
test transactions as real signed txs):

- ``submit_transactions`` — submit a test's transactions directly to a live
  devnet/testnet RPC where blocks are already produced (by the network's
  validators + a builder like buildoor). No local EL. Optionally tees every
  submitted transaction to a CSV via a recording proxy. This is the primary
  path for "run an EEST test against a devnet."

- ``export_transactions`` — for when there is NO running network: boot a
  throwaway geth from a generated genesis, drive block production via the
  Engine API, capture the transactions to a CSV, and emit the genesis so a
  consumer can boot a compatible EL and replay them.
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import signal
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from execution_testing import EOA
from execution_testing.base_types import Bytes

from .devnet_genesis import DEFAULT_PREFUND_WEI, build_devnet_genesis
from .el import geth
from .rpc_proxy import DEFAULT_USER_AGENT, recording_proxy, split_basic_auth

# Devnet seed key (genesis-prefunded). Matches buildoor's devnet wallet key so
# the emitted genesis is consistent with buildoor's .hack/devnet conventions.
DEFAULT_SEED_KEY = (
    "0x04b9f63ecf84210c5366c66d68fa1f5da1fa4f634fad6dfc86178e4d79ff9e59"
)
DEFAULT_SEED_ADDR = "0xaff0ca253b97e54440965855cec0a8a2e2399896"
DEFAULT_CHAIN_ID = 7928
# Fixed EOA derivation start → reproducible transaction sequences.
# EEST parses this as a decimal int private key (well below the secp256k1
# order), incrementing it for each EOA a test allocates.
DEFAULT_EOA_START = str(
    0x1100000000000000000000000000000000000000000000000000000000000001
)

CSV_COLUMNS = [
    "seq",
    "block_number",
    "tx_index",
    "tx_hash",
    "type",
    "from",
    "to",
    "nonce",
    "value",
    "gas",
    "gas_price",
    "max_fee_per_gas",
    "input_len",
    "raw",
]


@dataclass
class ExportResult:
    """Outcome of an export run."""

    test_selector: str
    fork: str
    chain_id: int
    tx_count: int
    csv_path: Path
    genesis_path: Path
    meta_path: Path
    execute_ok: bool
    error: str | None = None


def export_transactions(
    test_selector: str,
    fork: str,
    output_dir: Path,
    specs_dir: Path,
    work_dir: Path,
    chain_id: int = DEFAULT_CHAIN_ID,
    seed_key: str = DEFAULT_SEED_KEY,
    seed_addr: str | None = None,
    eoa_start: str = DEFAULT_EOA_START,
    tx_wait_timeout: int = 60,
    include_benchmark: bool = False,
    gas_benchmark_values: str | None = None,
    k_filter: str | None = None,
) -> ExportResult:
    """Run one export and return its result."""
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    # Derive the prefunded address from the key so the genesis always funds
    # the account `execute` will actually spend from.
    if seed_addr is None:
        seed_addr = address_from_key(seed_key)

    genesis = build_devnet_genesis(
        fork=fork,
        chain_id=chain_id,
        prefunded={seed_addr: DEFAULT_PREFUND_WEI},
    )
    genesis_path = output_dir / "genesis.json"
    genesis_path.write_text(json.dumps(genesis, indent=2))

    csv_path = output_dir / "transactions.csv"
    meta_path = output_dir / "meta.json"

    with geth(genesis, work_dir) as handles:
        with recording_proxy(handles.rpc_url) as (proxy_url, recorder):
            execute_ok, err = _run_execute(
                specs_dir=specs_dir,
                test_selector=test_selector,
                fork=fork,
                chain_id=chain_id,
                seed_key=seed_key,
                eoa_start=eoa_start,
                rpc_url=proxy_url,
                engine_url=handles.auth_url,
                jwt_path=handles.jwt_secret_path,
                tx_wait_timeout=tx_wait_timeout,
                include_benchmark=include_benchmark,
                gas_benchmark_values=gas_benchmark_values,
                k_filter=k_filter,
            )
            raw_txs = recorder.snapshot()
            rows = _enrich(raw_txs, handles.rpc_url)

    # A green pytest run that captured zero transactions means every selected
    # test was skipped (e.g. mutable pre-alloc, fork mismatch) or matched
    # nothing — not a real export. Report that honestly rather than emitting a
    # header-only CSV under a success flag.
    if execute_ok and not rows:
        execute_ok = False
        if err is None:
            err = (
                "execute exited 0 but captured 0 transactions "
                "(test skipped, filtered out, or selected nothing)"
            )

    _write_csv(csv_path, rows)
    meta = {
        "test_selector": test_selector,
        "fork": fork,
        "chain_id": chain_id,
        "seed_address": seed_addr,
        "eoa_start": eoa_start,
        "tx_count": len(rows),
        "execute_ok": execute_ok,
        "error": err,
        "genesis": "genesis.json",
        "transactions": "transactions.csv",
        "note": (
            "Replay transactions.csv in `seq` order against an EL booted from "
            "genesis.json (same chain_id, seed prefunded). The first txs are "
            "setup (deterministic factory + funding + contract deploys); the "
            "rest are the test transactions."
        ),
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    return ExportResult(
        test_selector=test_selector,
        fork=fork,
        chain_id=chain_id,
        tx_count=len(rows),
        csv_path=csv_path,
        genesis_path=genesis_path,
        meta_path=meta_path,
        execute_ok=execute_ok,
        error=err,
    )


@dataclass
class SubmitResult:
    """Outcome of submitting a test's transactions to a live network."""

    test_selector: str
    fork: str
    chain_id: int
    rpc_url: str
    execute_ok: bool
    submitted_count: int | None  # None when not recorded (no --csv)
    csv_path: Path | None = None
    error: str | None = None


def submit_transactions(
    test_selector: str,
    fork: str,
    rpc_url: str,
    chain_id: int,
    specs_dir: Path,
    seed_key: str = DEFAULT_SEED_KEY,
    eoa_start: str | None = None,
    k_filter: str | None = None,
    csv_path: Path | None = None,
    tx_wait_timeout: int = 120,
    include_benchmark: bool = False,
    gas_benchmark_values: str | None = None,
    gas_price: int | None = None,
    max_fee_per_gas: int | None = None,
    max_priority_fee_per_gas: int | None = None,
) -> SubmitResult:
    """
    Submit a spec test's transactions to a live devnet/testnet RPC.

    The target network must already produce blocks (validators + builder), so
    no Engine API / local EL is involved — execute just broadcasts the txs and
    polls for inclusion. ``seed_key`` must be funded on the target network.
    When ``csv_path`` is given, every submitted transaction is also recorded.

    ``eoa_start`` seeds the ephemeral EOA keys each test allocates. It defaults
    to a RANDOM value per invocation so repeated/batched submits never reuse an
    EOA (a reused EOA carries a stale nonce → the test tx is rejected). Pass an
    explicit value only when you need reproducible EOA addresses.

    Pass explicit gas prices (wei) on networks where the RPC reports a zero
    priority fee — otherwise execute derives a max-fee of 0 and the txs are
    rejected below the base fee.
    """
    if eoa_start is None:
        eoa_start = random_eoa_start()
    common = dict(
        specs_dir=specs_dir,
        test_selector=test_selector,
        fork=fork,
        chain_id=chain_id,
        seed_key=seed_key,
        eoa_start=eoa_start,
        tx_wait_timeout=tx_wait_timeout,
        include_benchmark=include_benchmark,
        gas_benchmark_values=gas_benchmark_values,
        k_filter=k_filter,
        gas_price=gas_price,
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
    )

    if csv_path is not None:
        with recording_proxy(rpc_url) as (proxy_url, recorder):
            execute_ok, err = _run_execute(rpc_url=proxy_url, **common)
            raw_txs = recorder.snapshot()
        rows = _enrich(raw_txs, rpc_url)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        _write_csv(csv_path, rows)
        count: int | None = len(rows)
    else:
        execute_ok, err = _run_execute(rpc_url=rpc_url, **common)
        count = None

    return SubmitResult(
        test_selector=test_selector,
        fork=fork,
        chain_id=chain_id,
        rpc_url=rpc_url,
        execute_ok=execute_ok,
        submitted_count=count,
        csv_path=csv_path,
        error=err,
    )


def _run_execute(
    *,
    specs_dir: Path,
    test_selector: str,
    fork: str,
    chain_id: int,
    seed_key: str,
    eoa_start: str,
    rpc_url: str,
    tx_wait_timeout: int,
    engine_url: str | None = None,
    jwt_path: Path | None = None,
    include_benchmark: bool = False,
    gas_benchmark_values: str | None = None,
    k_filter: str | None = None,
    skip_cleanup: bool = True,
    gas_price: int | None = None,
    max_fee_per_gas: int | None = None,
    max_priority_fee_per_gas: int | None = None,
) -> tuple[bool, str | None]:
    """Invoke `execute remote` in the execution-specs venv as a subprocess.

    When ``engine_url`` is set, EEST drives block production itself via the
    Engine API (needed against an isolated EL with no consensus layer). When
    it is None, blocks are produced by the target network (a live testnet or a
    kurtosis devnet) and execute merely submits and polls for inclusion.
    """
    cmd = [
        "uv", "run", "execute", "remote",
        "--fork", fork,
        "--rpc-endpoint", rpc_url,
        "--chain-id", str(chain_id),
        "--rpc-seed-key", seed_key,
        "--eoa-start", eoa_start,
        "--tx-wait-timeout", str(tx_wait_timeout),
        "-p", "no:randomly",
    ]
    if engine_url is not None:
        cmd += ["--engine-endpoint", engine_url]
        if jwt_path is not None:
            cmd += ["--engine-jwt-secret-file", str(jwt_path)]
    if skip_cleanup:
        cmd.append("--skip-cleanup")
    if gas_price is not None:
        cmd += ["--default-gas-price", str(gas_price)]
    if max_fee_per_gas is not None:
        cmd += ["--default-max-fee-per-gas", str(max_fee_per_gas)]
    if max_priority_fee_per_gas is not None:
        cmd += ["--default-max-priority-fee-per-gas",
                str(max_priority_fee_per_gas)]
    if include_benchmark:
        cmd.append("--include-benchmark")
    if gas_benchmark_values:
        cmd += ["--gas-benchmark-values", gas_benchmark_values]
    if k_filter:
        cmd += ["-k", k_filter]
    cmd.append(test_selector)

    # Drop VIRTUAL_ENV so `uv run` resolves the execution-specs project env
    # rather than the (active) eest-replay venv.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    timeout = tx_wait_timeout + 600
    # Run in its own process group so a timeout can kill the whole tree
    # (uv -> pytest); subprocess's own timeout only kills the direct child,
    # leaving the pytest grandchild orphaned and still driving block builds.
    proc = subprocess.Popen(
        cmd,
        cwd=str(specs_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.communicate()
        return False, f"execute timed out after {timeout}s"

    if proc.returncode != 0:
        tail = "\n".join((stdout + "\n" + stderr).splitlines()[-12:])
        return False, f"execute exited {proc.returncode}:\n{tail}"
    return True, None


def _enrich(raw_txs: List[str], rpc_url: str) -> List[Dict[str, Any]]:
    """Compute tx hashes and pull decoded metadata from geth, in order."""
    rows: List[Dict[str, Any]] = []
    for seq, raw in enumerate(raw_txs):
        digest = Bytes(bytes.fromhex(raw.removeprefix("0x"))).keccak256().hex()
        tx_hash = digest if digest.startswith("0x") else f"0x{digest}"
        info = _get_tx(rpc_url, tx_hash)
        rows.append(_row(seq, raw, tx_hash, info))
    return rows


def _row(seq: int, raw: str, tx_hash: str, info: Dict[str, Any] | None) -> Dict[str, Any]:
    info = info or {}
    data = info.get("input", "0x")
    return {
        "seq": seq,
        "block_number": _to_int(info.get("blockNumber")),
        "tx_index": _to_int(info.get("transactionIndex")),
        "tx_hash": tx_hash,
        "type": _to_int(info.get("type")),
        "from": info.get("from", ""),
        "to": info.get("to") or "",  # null = contract creation
        "nonce": _to_int(info.get("nonce")),
        "value": _to_int(info.get("value")),
        "gas": _to_int(info.get("gas")),
        "gas_price": _to_int(info.get("gasPrice")),
        "max_fee_per_gas": _to_int(info.get("maxFeePerGas")),
        "input_len": (len(data) - 2) // 2 if isinstance(data, str) else 0,
        "raw": raw if raw.startswith("0x") else f"0x{raw}",
    }


def _get_tx(rpc_url: str, tx_hash: str) -> Dict[str, Any] | None:
    clean_url, auth_headers = split_basic_auth(rpc_url)
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1,
         "method": "eth_getTransactionByHash", "params": [tx_hash]}
    ).encode()
    req = urllib.request.Request(
        clean_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
            **auth_headers,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("result")
    except Exception:  # noqa: BLE001
        return None


def address_from_key(priv_hex: str) -> str:
    """Derive the lowercase 0x address for a private key."""
    return str(EOA(key=int(priv_hex, 16)))


def random_eoa_start() -> str:
    """
    Return a random EOA derivation start (decimal int string).

    240 random bits keeps it well below the secp256k1 order while leaving room
    for the per-test offsets execute adds. Used so independent submits don't
    derive the same ephemeral EOAs (which would carry stale nonces).
    """
    import secrets

    return str(secrets.randbits(240) + 2**16)


def _to_int(hex_or_none: Any) -> int | None:
    if hex_or_none is None:
        return None
    if isinstance(hex_or_none, int):
        return hex_or_none
    try:
        return int(hex_or_none, 16)
    except (ValueError, TypeError):
        return None


def _write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
