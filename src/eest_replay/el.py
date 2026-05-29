"""EL lifecycle: spin up an EL via Docker against a fixture's genesis.

Defaults to the ethpandaops geth ``bal-devnet-2`` image, which has full
Amsterdam/EIP-7928 support. Reth latest (2.2.0) still reports "Unsupported
fork" for Amsterdam payloads, so geth is the smoother default today.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator

import urllib.request

DEFAULT_IMAGE = "ethpandaops/geth:bal-devnet-2"


@dataclass
class ELHandles:
    """Handles for an active EL instance."""

    container: str
    rpc_url: str
    auth_url: str
    jwt_secret_path: Path
    data_dir: Path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _write_jwt(path: Path) -> None:
    path.write_text(secrets.token_hex(32))
    path.chmod(0o600)


def _write_genesis(path: Path, genesis: Dict[str, Any]) -> None:
    path.write_text(json.dumps(genesis, indent=2))


@contextlib.contextmanager
def geth(
    genesis: Dict[str, Any],
    work_dir: Path,
    image: str = DEFAULT_IMAGE,
    container_name: str | None = None,
) -> Iterator[ELHandles]:
    """
    Start geth with the given genesis. Yields handles; tears down on exit.

    Uses Docker so the host doesn't need geth installed. Mounts ``work_dir``
    into the container, runs ``geth init`` to seed the datadir, then ``geth``
    with HTTP + Engine API enabled.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    genesis_path = work_dir / "genesis.json"
    jwt_path = work_dir / "jwt.hex"
    data_dir = work_dir / "datadir"
    _write_genesis(genesis_path, genesis)
    _write_jwt(jwt_path)
    data_dir.mkdir(exist_ok=True)

    rpc_port = _free_port()
    auth_port = _free_port()
    name = container_name or f"eest-replay-{secrets.token_hex(4)}"

    # geth init seeds the datadir with the given chainspec
    subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{work_dir}:/work",
            image,
            "init",
            "--datadir", "/work/datadir",
            "/work/genesis.json",
        ],
        check=True,
        capture_output=True,
    )

    # Geth's stdout is captured into work_dir/el.log so failure diagnoses
    # don't require re-running.
    log_path = work_dir / "el.log"
    log_file = log_path.open("w")
    proc = subprocess.Popen(
        [
            "docker", "run", "--rm",
            "--name", name,
            "-p", f"{rpc_port}:8545",
            "-p", f"{auth_port}:8551",
            "-v", f"{work_dir}:/work",
            image,
            "--datadir", "/work/datadir",
            "--http", "--http.addr", "0.0.0.0", "--http.port", "8545",
            "--http.api", "eth,net,web3,debug,txpool,engine",
            "--http.vhosts", "*",
            "--authrpc.addr", "0.0.0.0", "--authrpc.port", "8551",
            "--authrpc.vhosts", "*",
            "--authrpc.jwtsecret", "/work/jwt.hex",
            "--nodiscover",
            "--syncmode", "full",
            "--maxpeers", "0",
            # Fixture transactions can have arbitrary low gas prices. Geth
            # sanitizes 0 back to its default 1 gwei, so we use 1 wei — the
            # lowest value that survives sanitization and is below any
            # realistic fixture gas price.
            "--txpool.pricelimit", "1",
            "--miner.gasprice", "1",
            "--gpo.ignoreprice", "1",
            # By default geth tags blocks with a "geth/<version>" string in
            # extra_data, which makes block hashes diverge from fixtures. Use
            # empty extra_data so the produced block matches more closely.
            "--miner.extradata", "0x",
            # Geth refuses to start when free disk falls below 2 GB by
            # default, which trips easily on CI runners or after a few dozen
            # replay datadirs accumulate. We never persist much per fixture,
            # so disable the safety check.
            "--datadir.minfreedisk", "0",
            # EEST's `execute` deploys the deterministic-deployment factory via
            # Nick's keyless method, a pre-EIP-155 (unprotected) transaction.
            # Allow it on this throwaway local node.
            "--rpc.allow-unprotected-txs",
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    rpc_url = f"http://127.0.0.1:{rpc_port}"
    auth_url = f"http://127.0.0.1:{auth_port}"
    try:
        _wait_for_rpc(rpc_url)
        yield ELHandles(
            container=name,
            rpc_url=rpc_url,
            auth_url=auth_url,
            jwt_secret_path=jwt_path,
            data_dir=data_dir,
        )
    finally:
        subprocess.run(["docker", "stop", name], capture_output=True)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# Backwards-compatible alias for the runner.
el = geth


@contextlib.contextmanager
def geth_dev_datadir(
    datadir: Path,
    chain_id: int,
    image: str = DEFAULT_IMAGE,
    container_name: str | None = None,
    dev_period: int = 1,
    dev_gaslimit: int = 60_000_000,
) -> Iterator[str]:
    """
    Boot geth in --dev mode against a PRE-BUILT datadir (e.g. from state-actor).

    No ``geth init`` — the genesis block, chain config, and state root are
    already embedded in the DB. geth ``--dev`` self-mines (PoA, ``dev_period``
    second blocks), so no consensus layer is needed; the chain advances on its
    own and includes mempool transactions. Yields the HTTP RPC URL; tears the
    container down on exit.

    ``datadir`` must be the directory that CONTAINS ``geth/chaindata`` (geth
    appends ``geth/chaindata`` to ``--datadir``).
    """
    datadir = datadir.resolve()
    if not (datadir / "geth" / "chaindata").is_dir():
        raise FileNotFoundError(
            f"{datadir} does not contain geth/chaindata — point --datadir at "
            "the directory state-actor wrote (the parent of geth/chaindata)."
        )
    rpc_port = _free_port()
    name = container_name or f"eest-bloat-{secrets.token_hex(4)}"
    log_file = (datadir / "geth-dev.log").open("w")
    proc = subprocess.Popen(
        [
            "docker", "run", "--rm",
            "--name", name,
            "-p", f"{rpc_port}:8545",
            "-v", f"{datadir}:/data",
            image,
            "--datadir", "/data",
            "--db.engine", "pebble",
            "--networkid", str(chain_id),
            "--dev", "--dev.period", str(dev_period),
            "--dev.gaslimit", str(dev_gaslimit),
            "--http", "--http.addr", "0.0.0.0", "--http.port", "8545",
            "--http.api", "eth,net,web3,txpool,debug",
            "--http.vhosts", "*",
            "--rpc.allow-unprotected-txs",
            "--datadir.minfreedisk", "0",
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    rpc_url = f"http://127.0.0.1:{rpc_port}"
    try:
        _wait_for_rpc(rpc_url)
        yield rpc_url
    finally:
        subprocess.run(["docker", "stop", name], capture_output=True)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _wait_for_rpc(rpc_url: str, timeout_s: float = 30.0) -> None:
    """Poll eth_chainId until reth responds or we time out."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
    ).encode()
    headers = {"Content-Type": "application/json"}
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(rpc_url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - polling, any failure is fine
            last_err = exc
            time.sleep(0.25)
    raise RuntimeError(
        f"EL at {rpc_url} did not become ready within {timeout_s}s: {last_err}"
    )
