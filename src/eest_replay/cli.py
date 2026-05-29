"""eest-replay CLI."""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from pathlib import Path

import click

from .el import geth_dev_datadir
from .export import (
    DEFAULT_CHAIN_ID,
    DEFAULT_EOA_START,
    DEFAULT_SEED_KEY,
    address_from_key,
    export_transactions,
    submit_transactions,
)
from .fixture import discover_fixture_files, load_engine_fixtures
from .report import BatchReport
from .state_actor import run_state_actor
from .sweep import recover_funded_eoas, sweep_account
from .runner import FixtureResult, replay


@click.group()
def main() -> None:
    """Turn EEST spec tests into transactions, and replay fixtures."""


@main.command("run")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--work-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory for per-fixture genesis.json / JWT / datadir. "
    "Each fixture gets a unique subdirectory under it.",
)
@click.option(
    "--payload-build-time",
    type=float,
    default=1.0,
    help="Seconds to wait between forkchoiceUpdated(attrs) and getPayload.",
)
@click.option(
    "--buildoor",
    "buildoor_binary",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help=(
        "Path to the buildoor binary. When set, builds are driven through "
        "`buildoor simbuild` instead of calling getPayload directly."
    ),
)
@click.option(
    "--report-json",
    type=click.Path(path_type=Path),
    default=None,
    help="Write a JSON aggregate report to this path.",
)
@click.option(
    "--report-md",
    type=click.Path(path_type=Path),
    default=None,
    help="Write a Markdown aggregate report to this path.",
)
@click.option(
    "--stop-on-first-failure",
    is_flag=True,
    help="Stop the batch as soon as a fixture fails or errors.",
)
@click.option("-v", "--verbose", count=True, help="Increase log verbosity.")
def run_cmd(
    path: Path,
    work_dir: Path | None,
    payload_build_time: float,
    buildoor_binary: Path | None,
    report_json: Path | None,
    report_md: Path | None,
    stop_on_first_failure: bool,
    verbose: int,
) -> None:
    """Run every blockchain_test_engine fixture under PATH.

    PATH may be a single fixture JSON or a directory. Directories are
    walked for `blockchain_tests_engine/**/*.json`.
    """
    level = logging.WARNING - (verbose * 10)
    logging.basicConfig(
        level=max(level, logging.DEBUG),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    base_work_dir = work_dir or Path(tempfile.mkdtemp(prefix="eest-replay-"))
    base_work_dir.mkdir(parents=True, exist_ok=True)

    fixture_files = list(discover_fixture_files(path))
    if not fixture_files:
        click.echo(f"no fixture JSON files under {path}", err=True)
        sys.exit(1)

    report = BatchReport()
    t0 = time.monotonic()

    try:
        for file_idx, fixture_path in enumerate(fixture_files):
            for fixture_idx, (test_id, fixture) in enumerate(
                load_engine_fixtures(fixture_path)
            ):
                slug = f"{file_idx:04d}-{fixture_idx:02d}-{fixture_path.stem}"
                per_dir = base_work_dir / slug
                click.echo(f"=== {test_id}")
                try:
                    result = replay(
                        fixture=fixture,
                        test_id=test_id,
                        work_dir=per_dir,
                        payload_build_time_s=payload_build_time,
                        buildoor_binary=buildoor_binary,
                    )
                except Exception as exc:  # noqa: BLE001
                    result = FixtureResult(
                        test_id=test_id,
                        blocks=[],
                        error=f"runner crashed: {exc!r}",
                    )

                report.add(result)
                _print_result(result)

                if stop_on_first_failure and not result.passed:
                    break
            if stop_on_first_failure and report.failed + report.errored:
                break
    finally:
        wall = time.monotonic() - t0
        click.echo(
            f"\n{report.passed}/{report.total} passed in {wall:.1f}s "
            f"({report.failed} failed, {report.errored} errored)"
        )
        if report_json is not None:
            report.write_json(report_json)
            click.echo(f"json report: {report_json}")
        if report_md is not None:
            report.write_markdown(report_md)
            click.echo(f"markdown report: {report_md}")

    sys.exit(0 if report.failed + report.errored == 0 else 1)


def _print_result(result: FixtureResult) -> None:
    if result.error:
        click.echo(f"  ERROR ({result.elapsed_s:.1f}s): {result.error}")
        return
    for block in result.blocks:
        tag = "OK" if block.matched else "MISMATCH"
        click.echo(f"  block {block.block_number}: {tag}")
        for m in block.mismatches:
            click.echo(f"    - {m}")
    if not result.blocks:
        click.echo("  (no blocks)")


@main.command("export")
@click.argument("test_selector")
@click.option(
    "--fork",
    required=True,
    help="Fork the test targets (e.g. Prague, Amsterdam). Must match the "
    "live network's active fork; here it sets the devnet genesis.",
)
@click.option(
    "--output",
    "output_dir",
    type=click.Path(path_type=Path),
    default=Path("export"),
    help="Directory for transactions.csv, genesis.json, meta.json.",
)
@click.option(
    "--specs-dir",
    type=click.Path(exists=True, path_type=Path),
    default=Path("../execution-specs"),
    help="Path to the execution-specs checkout (provides `execute` + tests).",
)
@click.option(
    "--work-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Scratch dir for the throwaway geth (default: a fresh tempdir).",
)
@click.option("--chain-id", type=int, default=DEFAULT_CHAIN_ID, show_default=True)
@click.option(
    "--seed-key",
    default=DEFAULT_SEED_KEY,
    help="Genesis-prefunded private key that funds the test (devnet only).",
)
@click.option("--eoa-start", default=DEFAULT_EOA_START, show_default=True,
              help="Fixed EOA derivation start for reproducible sequences.")
@click.option("--tx-wait-timeout", type=int, default=60, show_default=True)
@click.option("--include-benchmark", is_flag=True,
              help="Allow tests/benchmark/ selection.")
@click.option("--gas-benchmark-values", default=None,
              help="Gas limits in WHOLE millions for benchmark tests, e.g. "
              "'1' or '8,16'. Overrides the default. (For the fork's exact max "
              "per-tx gas use --transaction-gas-limit instead.)")
@click.option("--transaction-gas-limit", type=int, default=None,
              help="Raw per-tx gas limit (exact, not whole-millions) for "
              "benchmark tests. Default for benchmark runs: the fork's per-tx "
              "cap (2**24 = 16,777,216 on Osaka+), so the test tx lands on "
              "exactly the max. Overrides --gas-benchmark-values.")
@click.option("-k", "k_filter", default=None,
              help="pytest -k filter passed through to `execute remote`.")
@click.option("-v", "--verbose", count=True)
def export_cmd(
    test_selector: str,
    fork: str,
    output_dir: Path,
    specs_dir: Path,
    work_dir: Path | None,
    chain_id: int,
    seed_key: str,
    eoa_start: str,
    tx_wait_timeout: int,
    include_benchmark: bool,
    gas_benchmark_values: str | None,
    transaction_gas_limit: int | None,
    k_filter: str | None,
    verbose: int,
) -> None:
    """Convert a spec test into a CSV of signed transactions (setup + test).

    TEST_SELECTOR is a pytest selector understood by `execute remote`, e.g.
    'tests/frontier/opcodes/test_push.py::test_push[fork_Prague-state_test-PUSH1]'
    or a path plus -k. Setup transactions (deterministic factory, funding,
    contract deploys) are captured first, then the test transactions.
    """
    level = logging.WARNING - (verbose * 10)
    logging.basicConfig(level=max(level, logging.DEBUG))

    seed_addr = address_from_key(seed_key)
    scratch = work_dir or Path(tempfile.mkdtemp(prefix="eest-export-"))

    click.echo(f"=== exporting: {test_selector}")
    click.echo(f"    fork={fork} chain_id={chain_id} seed={seed_addr}")
    result = export_transactions(
        test_selector=test_selector,
        fork=fork,
        output_dir=output_dir,
        specs_dir=specs_dir,
        work_dir=scratch,
        chain_id=chain_id,
        seed_key=seed_key,
        seed_addr=seed_addr,
        eoa_start=eoa_start,
        tx_wait_timeout=tx_wait_timeout,
        include_benchmark=include_benchmark,
        gas_benchmark_values=gas_benchmark_values,
        transaction_gas_limit=transaction_gas_limit,
        k_filter=k_filter,
    )

    if not result.execute_ok:
        click.echo(f"  execute did not pass: {result.error}")
    click.echo(f"  captured {result.tx_count} transactions")
    click.echo(f"  csv:     {result.csv_path}")
    click.echo(f"  genesis: {result.genesis_path}")
    click.echo(f"  meta:    {result.meta_path}")
    sys.exit(0 if result.execute_ok and result.tx_count > 0 else 1)


@main.command("submit")
@click.argument("test_selector")
@click.option("--fork", required=True,
              help="Fork the target network is on (e.g. Prague, Amsterdam).")
@click.option("--rpc", "rpc_url", required=True,
              help="Target devnet/testnet EL JSON-RPC endpoint.")
@click.option("--chain-id", type=int, required=True,
              help="Chain id of the target network (cross-checked vs the RPC).")
@click.option(
    "--seed-key",
    default=DEFAULT_SEED_KEY,
    help="Private key FUNDED ON THE TARGET NETWORK that funds the test. "
    "The default is only prefunded on local devnets; pass your own for a "
    "testnet.",
)
@click.option(
    "--specs-dir",
    type=click.Path(exists=True, path_type=Path),
    default=Path("../execution-specs"),
    help="Path to the execution-specs checkout (provides `execute` + tests).",
)
@click.option("-k", "k_filter", default=None,
              help="pytest -k filter passed through to `execute remote`.")
@click.option(
    "--csv",
    "csv_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Also record every submitted transaction to this CSV.",
)
@click.option("--eoa-start", default=None,
              help="EOA derivation start (default: random per run, so repeated "
              "submits never reuse an ephemeral EOA). Set for reproducibility.")
@click.option("--tx-wait-timeout", type=int, default=120, show_default=True,
              help="Max seconds to wait for each tx to be included.")
@click.option("--include-benchmark", is_flag=True,
              help="Allow tests/benchmark/ selection.")
@click.option("--gas-benchmark-values", default=None,
              help="Gas limits in WHOLE millions for benchmark tests, e.g. "
              "'1' or '8,16'. Overrides the default. (For the fork's exact max "
              "per-tx gas use --transaction-gas-limit instead.)")
@click.option("--transaction-gas-limit", type=int, default=None,
              help="Raw per-tx gas limit (exact, not whole-millions) for "
              "benchmark tests. Default for benchmark runs: the fork's per-tx "
              "cap (2**24 = 16,777,216 on Osaka+), so the test tx lands on "
              "exactly the max. Overrides --gas-benchmark-values.")
@click.option("--gas-price", type=int, default=None,
              help="Legacy gas price in WEI (set on nets with ~0 fees).")
@click.option("--max-fee-per-gas", type=int, default=None,
              help="EIP-1559 max fee per gas in WEI. Set this on a network "
              "whose RPC reports a 0 priority fee, or txs are rejected below "
              "base fee. e.g. 5000000000 (5 gwei).")
@click.option("--max-priority-fee-per-gas", type=int, default=None,
              help="EIP-1559 max priority fee per gas in WEI, e.g. 1000000000.")
@click.option("--cleanup/--no-cleanup", default=True, show_default=True,
              help="Refund the funded test EOAs back to the seed after the "
              "test (reclaims funding; net spend ≈ gas). --no-cleanup leaves "
              "the ETH in the EOAs — recoverable later via `recover`.")
@click.option("--min-seed-balance", type=float, default=None,
              help="Safeguard: abort before running if the seed holds fewer "
              "than this many ETH (don't drain a low account).")
@click.option("--meta-out", type=click.Path(path_type=Path), default=None,
              help="Where to write the recovery sidecar (records eoa-start). "
              "Defaults next to --csv.")
@click.option("-v", "--verbose", count=True)
def submit_cmd(
    test_selector: str,
    fork: str,
    rpc_url: str,
    chain_id: int,
    seed_key: str,
    specs_dir: Path,
    k_filter: str | None,
    csv_path: Path | None,
    eoa_start: str,
    tx_wait_timeout: int,
    include_benchmark: bool,
    gas_benchmark_values: str | None,
    transaction_gas_limit: int | None,
    gas_price: int | None,
    max_fee_per_gas: int | None,
    max_priority_fee_per_gas: int | None,
    cleanup: bool,
    min_seed_balance: float | None,
    meta_out: Path | None,
    verbose: int,
) -> None:
    """Submit a spec test's transactions to a live devnet/testnet.

    Points EEST `execute remote` straight at --rpc. The network (its
    validators + builder, e.g. buildoor) produces the blocks; execute funds
    the sender, deploys the test's contracts, and broadcasts the test
    transactions into the mempool. Works against a long-living testnet or a
    local kurtosis devnet alike — no local EL needed.

    TEST_SELECTOR is a pytest path/node-id; combine with -k to narrow.
    """
    level = logging.WARNING - (verbose * 10)
    logging.basicConfig(level=max(level, logging.DEBUG))

    click.echo(f"=== submitting: {test_selector}")
    click.echo(f"    fork={fork} chain_id={chain_id} rpc={rpc_url}")
    click.echo(f"    seed={address_from_key(seed_key)}")
    result = submit_transactions(
        test_selector=test_selector,
        fork=fork,
        rpc_url=rpc_url,
        chain_id=chain_id,
        specs_dir=specs_dir,
        seed_key=seed_key,
        eoa_start=eoa_start,
        k_filter=k_filter,
        csv_path=csv_path,
        tx_wait_timeout=tx_wait_timeout,
        include_benchmark=include_benchmark,
        gas_benchmark_values=gas_benchmark_values,
        transaction_gas_limit=transaction_gas_limit,
        gas_price=gas_price,
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
        cleanup=cleanup,
        min_seed_balance_wei=(
            int(min_seed_balance * 10**18) if min_seed_balance is not None else None
        ),
        meta_path=meta_out,
    )
    _report_submit_result(result)
    sys.exit(0 if result.execute_ok else 1)


def _report_submit_result(result) -> None:
    """Print a SubmitResult: counts, spend, recovery sidecar, pass/fail."""
    if result.submitted_count is not None:
        click.echo(f"  submitted {result.submitted_count} transactions")
        if result.csv_path:
            click.echo(f"  csv: {result.csv_path}")
    if result.spent_wei is not None:
        click.echo(f"  seed spent: {result.spent_wei / 10**18:.6f} ETH")
    if result.meta_path is not None:
        click.echo(f"  recovery sidecar: {result.meta_path} "
                   "(run `eest-replay recover --meta <it> --to <addr>`)")
    if result.execute_ok:
        click.echo("  execute passed (txs included on-chain)")
    else:
        # Submission may still have happened — execute also verifies post-state,
        # which we don't require. Surface the outcome honestly.
        click.echo(f"  execute did not pass: {result.error}")


@main.command("bloat")
@click.argument("test_selector")
@click.option("--fork", required=True,
              help="Fork to run (e.g. Osaka). Also sets state-actor's genesis "
              "fork when generating the datadir.")
@click.option("--chain-id", type=int, default=1337, show_default=True,
              help="Chain id (state-actor default is 1337).")
@click.option(
    "--state-actor",
    "state_actor_bin",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to the state-actor binary. When set, generate a fresh bloated "
    "datadir (prefunding the seed) before running.",
)
@click.option("--target-size", default="200MB", show_default=True,
              help="Bloat size for state-actor generation (e.g. '5GB').")
@click.option(
    "--datadir",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Use an EXISTING state-actor geth datadir (parent of geth/chaindata) "
    "instead of generating one. The seed must be funded in it.",
)
@click.option(
    "--work-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to generate the datadir (default: a fresh tempdir).",
)
@click.option("--seed-key", default=DEFAULT_SEED_KEY,
              help="Seed key; auto-prefunded in the generated bloated state.")
@click.option(
    "--specs-dir",
    type=click.Path(exists=True, path_type=Path),
    default=Path("../execution-specs"),
    help="Path to the execution-specs checkout.",
)
@click.option("-k", "k_filter", default=None,
              help="pytest -k filter passed through to `execute remote`.")
@click.option("--csv", "csv_path", type=click.Path(path_type=Path), default=None,
              help="Record every submitted transaction to this CSV.")
@click.option("--tx-wait-timeout", type=int, default=120, show_default=True)
@click.option("--include-benchmark", is_flag=True)
@click.option("--gas-benchmark-values", default=None)
@click.option("--transaction-gas-limit", type=int, default=None,
              help="Raw per-tx gas limit for benchmark tests; defaults to the "
              "fork's per-tx cap (2**24 on Osaka+).")
@click.option("--max-fee-per-gas", type=int, default=None)
@click.option("--max-priority-fee-per-gas", type=int, default=None)
@click.option("-v", "--verbose", count=True)
def bloat_cmd(
    test_selector: str,
    fork: str,
    chain_id: int,
    state_actor_bin: Path | None,
    target_size: str,
    datadir: Path | None,
    work_dir: Path | None,
    seed_key: str,
    specs_dir: Path,
    k_filter: str | None,
    csv_path: Path | None,
    tx_wait_timeout: int,
    include_benchmark: bool,
    gas_benchmark_values: str | None,
    transaction_gas_limit: int | None,
    max_fee_per_gas: int | None,
    max_priority_fee_per_gas: int | None,
    verbose: int,
) -> None:
    """Run a spec test against a state-actor-bloated local chain.

    Generates (or reuses) a bloated geth datadir, boots geth in --dev mode
    against it (self-mining, no consensus layer), and submits the test's
    transactions so they execute on top of the bloated state.
    """
    level = logging.WARNING - (verbose * 10)
    logging.basicConfig(level=max(level, logging.DEBUG))

    seed_addr = address_from_key(seed_key)

    if datadir is None and state_actor_bin is None:
        raise click.UsageError("pass --state-actor <bin> to generate a datadir, "
                               "or --datadir <existing> to reuse one.")

    if state_actor_bin is not None:
        gen_dir = work_dir or Path(tempfile.mkdtemp(prefix="eest-bloat-"))
        click.echo(f"=== generating bloated datadir (target {target_size}, "
                   f"fork {fork}, chain {chain_id}, seed {seed_addr}) ...")
        datadir = run_state_actor(
            binary=state_actor_bin, db_dir=gen_dir, seed_addr=seed_addr,
            chain_id=chain_id, fork=fork, target_size=target_size,
        )
        click.echo(f"    datadir: {datadir}")

    click.echo(f"=== booting geth --dev on bloated state, submitting: {test_selector}")
    with geth_dev_datadir(datadir, chain_id) as rpc_url:
        result = submit_transactions(
            test_selector=test_selector,
            fork=fork,
            rpc_url=rpc_url,
            chain_id=chain_id,
            specs_dir=specs_dir,
            seed_key=seed_key,
            k_filter=k_filter,
            csv_path=csv_path,
            tx_wait_timeout=tx_wait_timeout,
            include_benchmark=include_benchmark,
            gas_benchmark_values=gas_benchmark_values,
            transaction_gas_limit=transaction_gas_limit,
            max_fee_per_gas=max_fee_per_gas,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
            # The bloated --dev chain is throwaway and the seed has 1M ETH, so
            # there's nothing to reclaim — skip the refund phase.
            cleanup=False,
        )

    _report_submit_result(result)
    sys.exit(0 if result.execute_ok else 1)


@main.command("recover")
@click.option("--rpc", "rpc_url", required=True,
              help="RPC endpoint of the network to sweep on.")
@click.option("--to", "to_addr", required=True,
              help="Address to sweep recovered ETH into (e.g. your new seed).")
@click.option("--chain-id", type=int, required=True)
@click.option("--meta", type=click.Path(exists=True, path_type=Path), default=None,
              help="A recovery sidecar (.recovery.json) written by submit; "
              "reads eoa-start from it.")
@click.option("--eoa-start", default=None,
              help="EOA derivation start to recover from (alternative to --meta).")
@click.option("--key", default=None,
              help="Sweep a single account by its private key (hex).")
@click.option("--count", type=int, default=64, show_default=True,
              help="How many derived EOAs to scan from eoa-start.")
@click.option("--max-fee-per-gas", type=int, default=5_000_000_000, show_default=True)
@click.option("--max-priority-fee-per-gas", type=int, default=1_000_000_000,
              show_default=True)
@click.option("-v", "--verbose", count=True)
def recover_cmd(
    rpc_url: str,
    to_addr: str,
    chain_id: int,
    meta: Path | None,
    eoa_start: str | None,
    key: str | None,
    count: int,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
    verbose: int,
) -> None:
    """Sweep ETH from test EOAs (or a single key) back to an address.

    Reclaims ETH the funding phase left in per-test EOAs. Point it at a
    recovery sidecar (--meta) or give --eoa-start; EEST derived the EOAs as
    key = int(eoa-start) + i, so this scans --count keys and sweeps any that
    hold a balance. Use --key to sweep one specific account.
    """
    level = logging.WARNING - (verbose * 10)
    logging.basicConfig(level=max(level, logging.DEBUG))

    if key is not None:
        result = sweep_account(
            rpc_url, int(key, 16), to_addr, chain_id,
            max_fee_per_gas=max_fee_per_gas,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
        )
        if result is None:
            click.echo("  nothing to sweep (balance below gas reserve)")
            sys.exit(0)
        tx_hash, swept = result
        click.echo(f"  swept {swept / 10**18:.6f} ETH → {to_addr}  tx={tx_hash}")
        sys.exit(0)

    if eoa_start is None:
        if meta is None:
            raise click.UsageError("pass --meta <sidecar>, --eoa-start, or --key.")
        eoa_start = json.loads(meta.read_text())["eoa_start"]

    click.echo(f"=== recovering up to {count} EOAs from eoa-start → {to_addr}")
    recovered = recover_funded_eoas(
        rpc_url, int(eoa_start), to_addr, chain_id, count=count,
        max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
    )
    total = sum(r.swept_wei for r in recovered)
    for r in recovered:
        click.echo(f"  {r.address}: {r.swept_wei / 10**18:.6f} ETH  tx={r.tx_hash}")
    click.echo(f"  recovered {len(recovered)} accounts, {total / 10**18:.6f} ETH total")
