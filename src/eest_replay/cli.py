"""eest-replay CLI."""

from __future__ import annotations

import logging
import sys
import tempfile
import time
from pathlib import Path

import click

from .fixture import discover_fixture_files, load_engine_fixtures
from .report import BatchReport
from .runner import FixtureResult, replay


@click.group()
def main() -> None:
    """Replay EEST fixtures against a per-fixture EL."""


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
