"""Drive an EL through a BlockchainEngineFixture and validate built blocks."""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List

from execution_testing.fixtures.blockchain import (
    BlockchainEngineFixture,
    FixtureEngineNewPayload,
    FixtureExecutionPayload,
)
from execution_testing.rpc import (
    EngineRPC,
    EthRPC,
)
from execution_testing.rpc.rpc_types import (
    ForkchoiceState,
    PayloadAttributes,
    PayloadStatusEnum,
)

from .buildoor_client import buildoor_simbuild, request_build
from .chainspec import build_genesis
from .el import geth

logger = logging.getLogger(__name__)


# fork → engine_getPayloadVX version. newPayload/fcu versions stay as the
# fixture declares them, but getPayload has its own V-axis (V6 for Amsterdam).
_GET_PAYLOAD_VERSION_BY_FORK = {
    "Paris": 1,
    "Shanghai": 2,
    "Cancun": 3,
    "Prague": 4,
    "Osaka": 5,
    "Amsterdam": 6,
}


def _get_payload_version(fixture: BlockchainEngineFixture) -> int:
    """Pick engine_getPayloadVX based on the fixture's fork."""
    name = fixture.fork.name() if hasattr(fixture.fork, "name") else str(fixture.fork)
    return _GET_PAYLOAD_VERSION_BY_FORK.get(
        name, fixture.payloads[0].new_payload_version
    )


@dataclass
class BlockResult:
    """Outcome of one fixture block."""

    block_number: int
    matched: bool
    mismatches: List[str]


@dataclass
class FixtureResult:
    """Outcome of running a full fixture end to end."""

    test_id: str
    blocks: List[BlockResult]
    error: str | None = None
    fork: str | None = None
    elapsed_s: float = 0.0

    @property
    def passed(self) -> bool:
        return self.error is None and all(b.matched for b in self.blocks)


def _payload_attributes(payload: FixtureEngineNewPayload) -> PayloadAttributes:
    """Build PayloadAttributes for a forkchoiceUpdated triggering a build."""
    expected = payload.params[0]
    parent_beacon_block_root = (
        payload.params[2]
        if payload.forkchoice_updated_version >= 3 and len(payload.params) >= 3
        else None
    )
    return PayloadAttributes(
        timestamp=expected.timestamp,
        prev_randao=expected.prev_randao,
        suggested_fee_recipient=expected.fee_recipient,
        withdrawals=expected.withdrawals,
        parent_beacon_block_root=parent_beacon_block_root,
        slot_number=expected.slot_number,
    )


# Fields the builder controls itself (EIP-1559 gas-limit picking, extra_data
# tagging). block_hash then transitively differs. test_via_build.py ignores
# these for the same reason — we follow suit. When routing through buildoor,
# extra_data is also expected to differ because buildoor prepends "buildoor/".
_BUILDER_PICKED_FIELDS = {"gas_limit", "extra_data", "block_hash"}


def _diff_payload(
    built: FixtureExecutionPayload, expected: FixtureExecutionPayload
) -> List[str]:
    """Compare every field of FixtureExecutionPayload between built and expected."""
    mismatches: List[str] = []
    for field in FixtureExecutionPayload.model_fields:
        if field in _BUILDER_PICKED_FIELDS:
            continue
        bv = getattr(built, field, None)
        ev = getattr(expected, field, None)
        if bv != ev:
            mismatches.append(f"{field}: expected {ev!r}, got {bv!r}")
    return mismatches


def replay(
    fixture: BlockchainEngineFixture,
    test_id: str,
    work_dir: Path,
    payload_build_time_s: float = 1.0,
    buildoor_binary: Path | None = None,
) -> FixtureResult:
    """
    Run a fixture against a fresh EL, validate each built block.

    For each engine_newPayload in the fixture:
      1. submit txs to mempool via eth_sendRawTransaction
      2. ask the builder for a block:
         - buildoor_binary=None: forkchoiceUpdated(attrs) + getPayload directly
         - else: POST /build to a `buildoor simbuild` subprocess that drives
           the same engine calls + applies buildoor's extraData modifier
      3. diff built vs expected, record mismatches
      4. newPayload(expected) + fcu(expected.block_hash) to advance the chain
    """
    started_at = time.monotonic()
    fork_name = (
        fixture.fork.name()
        if hasattr(fixture.fork, "name")
        else str(fixture.fork)
    )
    genesis = build_genesis(fixture)
    get_payload_version = _get_payload_version(fixture)

    def _finish(result: FixtureResult) -> FixtureResult:
        result.fork = fork_name
        result.elapsed_s = round(time.monotonic() - started_at, 3)
        return result

    with geth(genesis, work_dir) as handles:
        jwt_secret = bytes.fromhex(handles.jwt_secret_path.read_text().strip())
        eth = EthRPC(handles.rpc_url)
        engine = EngineRPC(handles.auth_url, jwt_secret=jwt_secret)
        with contextlib.ExitStack() as stack:
            buildoor_base_url: str | None = None
            if buildoor_binary is not None:
                handles_b = stack.enter_context(
                    buildoor_simbuild(
                        binary=buildoor_binary,
                        el_engine_url=handles.auth_url,
                        jwt_secret_path=handles.jwt_secret_path,
                        work_dir=work_dir,
                        build_wait_ms=int(payload_build_time_s * 1000),
                    )
                )
                buildoor_base_url = handles_b.base_url
            return _finish(_drive_chain(
                fixture=fixture,
                test_id=test_id,
                eth=eth,
                engine=engine,
                get_payload_version=get_payload_version,
                payload_build_time_s=payload_build_time_s,
                buildoor_base_url=buildoor_base_url,
            ))


def _drive_chain(
    *,
    fixture: BlockchainEngineFixture,
    test_id: str,
    eth: EthRPC,
    engine: EngineRPC,
    get_payload_version: int,
    payload_build_time_s: float,
    buildoor_base_url: str | None,
) -> FixtureResult:
    block_results: List[BlockResult] = []

    # Bootstrap: genesis is the canonical head.
    first_version = fixture.payloads[0].forkchoice_updated_version
    bootstrap = engine.forkchoice_updated(
        forkchoice_state=ForkchoiceState(
            head_block_hash=fixture.genesis.block_hash,
        ),
        version=first_version,
    )
    if bootstrap.payload_status.status != PayloadStatusEnum.VALID:
        return FixtureResult(
            test_id=test_id,
            blocks=block_results,
            error=(
                "bootstrap forkchoiceUpdated not VALID: "
                f"{bootstrap.payload_status.status}"
            ),
        )

    for idx, payload in enumerate(fixture.payloads):
        if not payload.valid():
            logger.info("skipping invalid payload %d", idx)
            continue
        expected = payload.params[0]
        _submit_mempool(eth, expected.transactions)

        if buildoor_base_url is not None:
            built = request_build(
                base_url=buildoor_base_url,
                parent_hash=str(expected.parent_hash),
                payload=payload,
            )
        else:
            attrs = _payload_attributes(payload)
            fcu_resp = engine.forkchoice_updated(
                forkchoice_state=ForkchoiceState(
                    head_block_hash=expected.parent_hash,
                ),
                payload_attributes=attrs,
                version=payload.forkchoice_updated_version,
            )
            if fcu_resp.payload_status.status != PayloadStatusEnum.VALID:
                return FixtureResult(
                    test_id=test_id,
                    blocks=block_results,
                    error=(
                        f"fcu (block {idx}) not VALID: "
                        f"{fcu_resp.payload_status.status}"
                    ),
                )
            if fcu_resp.payload_id is None:
                return FixtureResult(
                    test_id=test_id,
                    blocks=block_results,
                    error=f"fcu (block {idx}) returned no payloadId",
                )
            time.sleep(payload_build_time_s)
            built = engine.get_payload(
                fcu_resp.payload_id, version=get_payload_version,
            ).execution_payload

        mismatches = _diff_payload(built, expected)
        block_results.append(
            BlockResult(
                block_number=int(expected.number),
                matched=not mismatches,
                mismatches=mismatches,
            )
        )

        # Advance the chain along the expected block, regardless of what the
        # builder produced (mirrors test_via_build.py).
        import_resp = engine.new_payload(
            *payload.params, version=payload.new_payload_version
        )
        if import_resp.status != PayloadStatusEnum.VALID:
            return FixtureResult(
                test_id=test_id,
                blocks=block_results,
                error=(
                    f"newPayload (block {idx}) not VALID: "
                    f"{import_resp.status}"
                ),
            )
        advance = engine.forkchoice_updated(
            forkchoice_state=ForkchoiceState(
                head_block_hash=expected.block_hash,
            ),
            version=payload.forkchoice_updated_version,
        )
        if advance.payload_status.status != PayloadStatusEnum.VALID:
            return FixtureResult(
                test_id=test_id,
                blocks=block_results,
                error=(
                    f"fcu advance (block {idx}) not VALID: "
                    f"{advance.payload_status.status}"
                ),
            )

    return FixtureResult(test_id=test_id, blocks=block_results)


def _submit_mempool(eth: EthRPC, raw_txs: List) -> None:
    """Submit each raw transaction via eth_sendRawTransaction."""
    for raw in raw_txs:
        try:
            tx_hash = eth.send_raw_transaction(raw)
            logger.info("submitted tx %s", tx_hash)
        except Exception as exc:  # noqa: BLE001
            logger.error("send_raw_transaction failed: %s", exc)
            raise
