from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "scripts" / "benchmark_hook.py"
FIXTURES = {"safe", "block", "stop", "custom", "adversarial"}


def _benchmark(scale: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            str(BENCHMARK),
            sys.executable,
            "--samples-per-fixture",
            "1",
            "--ceiling-scale",
            scale,
        ),
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )


def test_benchmark_covers_the_prompt_the_reply_and_the_adversarial_shapes() -> None:
    # One cold sample on a busy test machine is not what CI gates on.
    completed = _benchmark("100")
    result = json.loads(completed.stdout)

    assert completed.returncode == 0, completed.stderr
    assert result["sample_counts"] == dict.fromkeys(FIXTURES, 1)
    assert set(result["timings_ms"]) == FIXTURES
    assert set(result["ceilings_ms"]) == FIXTURES


def test_benchmark_fails_when_a_median_exceeds_its_ceiling() -> None:
    """The failure names each median and its ceiling, and the result is written
    first: a CI log that held only the fixture names could not say by how much."""
    completed = _benchmark("0.0001")
    result = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert completed.stderr == (
        "hook benchmark failed: median over its ceiling: "
        + ", ".join(
            f"{name} {result['timings_ms'][name]['p50_ms']} ms"
            f" > {result['ceilings_ms'][name]:g} ms"
            for name in sorted(FIXTURES)
        )
        + "\n"
    )
