"""Aggregate and serialize FixtureResults from a batch run."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from .runner import FixtureResult


@dataclass
class BatchReport:
    """Aggregate over many FixtureResults."""

    results: List[FixtureResult] = field(default_factory=list)

    def add(self, result: FixtureResult) -> None:
        self.results.append(result)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def errored(self) -> int:
        return sum(1 for r in self.results if r.error is not None)

    @property
    def elapsed_s(self) -> float:
        return round(sum(r.elapsed_s for r in self.results), 3)

    def to_dict(self) -> dict:
        return {
            "summary": {
                "total": self.total,
                "passed": self.passed,
                "failed": self.failed,
                "errored": self.errored,
                "elapsed_s": self.elapsed_s,
            },
            "results": [
                {
                    "test_id": r.test_id,
                    "fork": r.fork,
                    "elapsed_s": r.elapsed_s,
                    "passed": r.passed,
                    "error": r.error,
                    "blocks": [
                        {
                            "number": b.block_number,
                            "matched": b.matched,
                            "mismatches": b.mismatches,
                        }
                        for b in r.blocks
                    ],
                }
                for r in self.results
            ],
        }

    def write_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2))

    def write_markdown(self, path: Path) -> None:
        lines: List[str] = []
        lines.append("# eest-replay report")
        lines.append("")
        lines.append(
            f"{self.passed}/{self.total} fixtures passed "
            f"({self.failed} failed, {self.errored} errored) "
            f"in {self.elapsed_s}s wall-clock"
        )
        lines.append("")
        lines.append("| status | fork | time | test |")
        lines.append("|---|---|---|---|")
        for r in self.results:
            status = "PASS" if r.passed else ("ERR" if r.error else "FAIL")
            lines.append(
                f"| {status} | {r.fork or '-'} | {r.elapsed_s:.2f}s | "
                f"`{r.test_id}` |"
            )

        failures = [r for r in self.results if not r.passed]
        if failures:
            lines.append("")
            lines.append("## Failures")
            lines.append("")
            for r in failures:
                lines.append(f"### `{r.test_id}`")
                if r.error:
                    lines.append("")
                    lines.append(f"Error: {r.error}")
                for b in r.blocks:
                    if b.matched:
                        continue
                    lines.append("")
                    lines.append(f"Block {b.block_number} mismatches:")
                    for m in b.mismatches:
                        lines.append(f"  - {m}")
                lines.append("")
        path.write_text("\n".join(lines) + "\n")
