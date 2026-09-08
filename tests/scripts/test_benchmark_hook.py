from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "scripts" / "benchmark_hook.py"


def test_benchmark_covers_the_prompt_and_the_reply() -> None:
    completed = subprocess.run(
        (
            sys.executable,
            str(BENCHMARK),
            sys.executable,
            "--samples-per-fixture",
            "1",
        ),
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
        timeout=120,
    )
    result = json.loads(completed.stdout)

    assert result["sample_counts"] == {"safe": 1, "block": 1, "stop": 1}
    assert set(result["timings_ms"]) == {"safe", "block", "stop"}
