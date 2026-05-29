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
from .sweep import get_balance

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
    transaction_gas_limit: int | None = None,
    k_filter: str | None = None,
) -> ExportResult:
    """Run one export and return its result."""
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    # Derive the prefunded address from the key so the genesis always funds
    # the account `execute` will actually spend from.
    if seed_addr is None:
        seed_addr = address_from_key(seed_key)
    # Benchmark runs default to the fork's max per-tx gas; overridable.
    gas_benchmark_values, transaction_gas_limit = _resolve_benchmark_gas(
        fork, test_selector, include_benchmark,
        gas_benchmark_values, transaction_gas_limit,
    )

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
                transaction_gas_limit=transaction_gas_limit,
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
    spent_wei: int | None = None  # seed balance delta over the run
    meta_path: Path | None = None  # recovery sidecar (records eoa_start)


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
    transaction_gas_limit: int | None = None,
    gas_price: int | None = None,
    max_fee_per_gas: int | None = None,
    max_priority_fee_per_gas: int | None = None,
    cleanup: bool = True,
    min_seed_balance_wei: int | None = None,
    meta_path: Path | None = None,
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

    Safeguards (protect seed ETH):
    - ``min_seed_balance_wei``: pre-flight floor — abort before running if the
      seed holds less than this, so a near-empty account is never drained.
    - ``cleanup`` (default True): run EEST's refund phase so the funded test
      EOAs return their balance to the seed — net spend is then just gas.
    - the recovery sidecar (always written) records ``eoa_start`` so the funded
      EOAs can be re-derived and swept later even if cleanup is skipped.

    Pass explicit gas prices (wei) on networks where the RPC reports a zero
    priority fee — otherwise execute derives a max-fee of 0 and the txs are
    rejected below the base fee.
    """
    if eoa_start is None:
        eoa_start = random_eoa_start()
    seed_addr = address_from_key(seed_key)

    # Pre-flight spend guard: don't even start if the seed is below the floor.
    try:
        balance_before: int | None = get_balance(rpc_url, seed_addr)
    except Exception:  # noqa: BLE001 - balance is advisory; don't block on RPC hiccup
        balance_before = None
    if (
        min_seed_balance_wei is not None
        and balance_before is not None
        and balance_before < min_seed_balance_wei
    ):
        return SubmitResult(
            test_selector=test_selector, fork=fork, chain_id=chain_id,
            rpc_url=rpc_url, execute_ok=False, submitted_count=None,
            csv_path=csv_path,
            error=(
                f"aborted: seed {seed_addr} balance "
                f"{balance_before / 10**18:.6f} ETH is below the "
                f"--min-seed-balance floor of {min_seed_balance_wei / 10**18:.6f} ETH"
            ),
        )

    # Always record how to recover the funded EOAs (key = eoa_start + i).
    written_meta = _write_recovery_meta(
        meta_path, csv_path, seed_addr, eoa_start, chain_id, fork, rpc_url
    )

    # Benchmark runs default to the fork's max per-tx gas; --gas-benchmark-values
    # / --transaction-gas-limit override it.
    gas_benchmark_values, transaction_gas_limit = _resolve_benchmark_gas(
        fork, test_selector, include_benchmark,
        gas_benchmark_values, transaction_gas_limit,
    )
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
        transaction_gas_limit=transaction_gas_limit,
        k_filter=k_filter,
        gas_price=gas_price,
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
        skip_cleanup=not cleanup,
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

    spent: int | None = None
    if balance_before is not None:
        try:
            spent = balance_before - get_balance(rpc_url, seed_addr)
        except Exception:  # noqa: BLE001
            spent = None

    return SubmitResult(
        test_selector=test_selector,
        fork=fork,
        chain_id=chain_id,
        rpc_url=rpc_url,
        execute_ok=execute_ok,
        submitted_count=count,
        csv_path=csv_path,
        error=err,
        spent_wei=spent,
        meta_path=written_meta,
    )


def _write_recovery_meta(
    meta_path: Path | None,
    csv_path: Path | None,
    seed_addr: str,
    eoa_start: str,
    chain_id: int,
    fork: str,
    rpc_url: str,
) -> Path | None:
    """Write a sidecar recording eoa_start so funded EOAs can be recovered."""
    if meta_path is None:
        if csv_path is not None:
            meta_path = csv_path.with_suffix(".recovery.json")
        else:
            return None  # nowhere obvious to put it; caller can pass --meta-out
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({
        "seed_address": seed_addr,
        "eoa_start": eoa_start,
        "chain_id": chain_id,
        "fork": fork,
        "rpc_url": rpc_url,
        "note": (
            "EEST funded per-test EOAs with key = int(eoa_start) + i. To reclaim "
            "their balances: eest-replay recover --meta this-file --to <addr>."
        ),
    }, indent=2))
    return meta_path


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
    transaction_gas_limit: int | None = None,
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
    if transaction_gas_limit is not None:
        cmd += ["--transaction-gas-limit", str(transaction_gas_limit)]
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


# Whole-million benchmark gas used when the target fork has no per-tx gas cap
# (pre-Osaka). A hefty default that fits typical devnet/testnet block limits.
FALLBACK_BENCHMARK_GAS_MILLIONS = 16


def fork_tx_gas_cap(fork: str) -> int | None:
    """
    Return the fork's per-transaction gas cap, or None if it has none.

    From Osaka on, EIP-7825 caps a single transaction at 2**24 = 16,777,216
    gas; pre-Osaka forks have no cap and return None.
    """
    try:
        import execution_testing.forks as forks_mod

        fork_cls = getattr(forks_mod, fork, None)
        return fork_cls.transaction_gas_limit_cap() if fork_cls else None
    except Exception:  # noqa: BLE001 - never let fork lookup break a run
        return None


def default_gas_benchmark_values(fork: str) -> str:
    """
    Return the whole-millions gas-benchmark value targeting the fork's MAX tx
    gas, as a string for ``--gas-benchmark-values``.

    ``--gas-benchmark-values`` is expressed in WHOLE millions
    (``value * 1_000_000``), so for a capped fork exactly 2**24 isn't
    expressible and 17M would exceed the cap and be rejected; we use
    ``floor(cap / 1_000_000)`` = 16, i.e. 16,000,000 gas. Forks without a
    per-tx cap fall back to a large default.

    NOTE: this whole-millions path is only the fallback for pre-cap forks. On
    a capped fork (Osaka+) benchmark runs instead default ``--transaction-gas-
    limit`` to the EXACT cap (see ``_resolve_benchmark_gas``), so the test tx
    lands on exactly 2**24 rather than the 16M floor.
    """
    cap = fork_tx_gas_cap(fork)
    millions = (cap // 1_000_000) if cap else FALLBACK_BENCHMARK_GAS_MILLIONS
    return str(millions)


def _resolve_benchmark_gas(
    fork: str,
    test_selector: str,
    include_benchmark: bool,
    gas_benchmark_values: str | None,
    transaction_gas_limit: int | None,
) -> tuple[str | None, int | None]:
    """
    Default a benchmark run's per-tx gas to the fork's MAX when the caller gave
    no explicit gas override.

    On a capped fork (Osaka+), default ``--transaction-gas-limit`` to the exact
    per-tx cap (e.g. 2**24): EEST's ``tx_gas_limit`` fixture already returns the
    cap there, and for whole-millions ``gas_benchmark_value`` tests this raises
    the default block gas (and hence their gas budget) to the cap too — so both
    test families emit a tx at exactly 2**24. On a pre-cap fork, fall back to
    the whole-millions ``--gas-benchmark-values`` default. Explicit
    ``gas_benchmark_values`` or ``transaction_gas_limit`` always win.
    """
    if (
        gas_benchmark_values is None
        and transaction_gas_limit is None
        and _is_benchmark_run(test_selector, include_benchmark)
    ):
        cap = fork_tx_gas_cap(fork)
        if cap is not None:
            transaction_gas_limit = cap
        else:
            gas_benchmark_values = default_gas_benchmark_values(fork)
    return gas_benchmark_values, transaction_gas_limit


def _is_benchmark_run(test_selector: str, include_benchmark: bool) -> bool:
    """Whether this run targets benchmark tests (which honor gas values)."""
    return include_benchmark or "tests/benchmark" in test_selector


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
