"""Load `BlockchainEngineFixture` JSON files produced by `just fill`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Tuple

from execution_testing.fixtures.blockchain import BlockchainEngineFixture


def load_engine_fixtures(
    path: Path,
) -> Iterator[Tuple[str, BlockchainEngineFixture]]:
    """
    Yield (test_id, fixture) pairs from a single fixture JSON file.

    EEST emits one JSON file per test parametrization, but the file maps
    pytest IDs (one or more) to fixture payloads, so this is an iterator.
    """
    raw = json.loads(path.read_text())
    for test_id, payload in raw.items():
        fmt = payload.get("_info", {}).get("fixture-format")
        if fmt != "blockchain_test_engine":
            continue
        yield test_id, BlockchainEngineFixture.model_validate(payload)


def load_first_engine_fixture(
    path: Path,
) -> Tuple[str, BlockchainEngineFixture]:
    """Convenience for the single-fixture case used in early slices."""
    for test_id, fixture in load_engine_fixtures(path):
        return test_id, fixture
    raise ValueError(f"No blockchain_test_engine fixture found in {path}")


def discover_fixture_files(path: Path) -> Iterator[Path]:
    """
    Yield fixture JSON paths under ``path``.

    If ``path`` is a single .json file, yield it. If it's a directory, walk
    recursively and yield every .json file under ``blockchain_tests_engine``
    (or, if the path is already inside such a tree, every .json under it).
    """
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        raise FileNotFoundError(path)
    # If the user pointed at a directory that contains the standard
    # `blockchain_tests_engine` tree, walk just that subtree to avoid pulling
    # in `blockchain_tests` (the non-engine fixtures we skip anyway).
    engine_root = path / "blockchain_tests_engine"
    root = engine_root if engine_root.is_dir() else path
    for json_file in sorted(root.rglob("*.json")):
        # Skip EEST metadata JSON files emitted under `.meta/`.
        if any(part == ".meta" for part in json_file.parts):
            continue
        yield json_file
