"""Spawn `buildoor simbuild` and call its HTTP /build endpoint.

This wires the actual buildoor build pipeline (RequestPayloadBuild +
GetPayloadRaw + ModifyPayloadExtraData) into our replay loop, in place of
the direct Engine API calls in `runner.py`.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, List

from execution_testing.fixtures.blockchain import (
    FixtureEngineNewPayload,
    FixtureExecutionPayload,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@dataclass
class BuildoorHandles:
    """Handles for a running `buildoor simbuild` instance."""

    base_url: str
    process: subprocess.Popen


@contextlib.contextmanager
def buildoor_simbuild(
    binary: Path,
    el_engine_url: str,
    jwt_secret_path: Path,
    work_dir: Path,
    build_wait_ms: int = 500,
) -> Iterator[BuildoorHandles]:
    """Start `buildoor simbuild` and yield a base URL pointing at /build."""
    port = _free_port()
    log_path = work_dir / "buildoor.log"
    log_file = log_path.open("w")

    proc = subprocess.Popen(
        [
            str(binary),
            "simbuild",
            "--el-engine-api", el_engine_url,
            "--el-jwt-secret", str(jwt_secret_path),
            "--listen-addr", f":{port}",
            "--simbuild-build-wait-ms", str(build_wait_ms),
            "--log-level", "info",
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    base = f"http://127.0.0.1:{port}"
    try:
        _wait_for_health(base)
        yield BuildoorHandles(base_url=base, process=proc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()


def _wait_for_health(base_url: str, timeout_s: float = 15.0) -> None:
    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.25)
    raise RuntimeError(
        f"buildoor simbuild at {base_url} not ready in {timeout_s}s: {last_err}"
    )


def request_build(
    base_url: str,
    parent_hash: str,
    payload: FixtureEngineNewPayload,
) -> FixtureExecutionPayload:
    """POST /build and return the (parsed) ExecutionPayload buildoor produced."""
    expected = payload.params[0]
    parent_beacon = (
        payload.params[2]
        if payload.forkchoice_updated_version >= 3 and len(payload.params) >= 3
        else None
    )
    body: dict[str, Any] = {
        "parent_hash": parent_hash,
        "timestamp": _hex(expected.timestamp),
        "prev_randao": _hex(expected.prev_randao),
        "suggested_fee_recipient": _hex(expected.fee_recipient),
        "parent_beacon_block_root": _hex(parent_beacon) if parent_beacon is not None else "",
        "slot_number": _hex(expected.slot_number) if expected.slot_number is not None else "0x0",
        "target_gas_limit": "0x0",
        "withdrawals": [
            {
                "index": _hex(w.index),
                "validator_index": _hex(w.validator_index),
                "address": _hex(w.address),
                "amount": _hex(w.amount),
            }
            for w in (expected.withdrawals or [])
        ],
    }

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/build",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60.0) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"buildoor /build returned {e.code}: {detail}") from e

    parsed = json.loads(raw)
    return FixtureExecutionPayload.model_validate(parsed["execution_payload"])


def _hex(value: Any) -> str:
    if value is None:
        return "0x0"
    if isinstance(value, int):
        return hex(value)
    s = str(value)
    return s if s.startswith("0x") else f"0x{s}"
