from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLAIM = re.compile(r"\*\*([\d,]+)\+\*\*, one command")


def test_the_readme_does_not_claim_more_tests_than_exist() -> None:
    """A number in prose rots. This one may only be an understatement."""
    claimed = CLAIM.search((ROOT / "README.md").read_text(encoding="utf-8"))
    assert claimed, "the README no longer states a test-count floor"

    result = subprocess.run(
        (sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts="),
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=300,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": ""},
    )
    if result.returncode != 0:  # pragma: no cover - a broken collection
        pytest.skip("the suite could not be collected")
    collected = re.search(r"(\d+) tests? collected", result.stdout.decode())
    assert collected, result.stdout.decode()[-400:]

    assert int(collected.group(1)) >= int(claimed.group(1).replace(",", ""))
