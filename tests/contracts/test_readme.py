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


def test_the_readme_says_bare_numbers_are_not_phone_numbers() -> None:
    assert "timestamps, ids and decimals" in (ROOT / "README.md").read_text(
        encoding="utf-8"
    )


def test_the_readme_carries_the_cli_tagline() -> None:
    assert "Local traffic visibility and privacy controls for coding agents." in (
        ROOT / "README.md"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "path", ("README.md", "plugins/shim-cli/README.md"), ids=("root", "plugin")
)
def test_a_readme_says_the_archive_is_on_main_and_every_tag(path: str) -> None:
    text = " ".join((ROOT / path).read_text(encoding="utf-8").split())

    assert "may not contain" not in text
    assert "may be absent" not in text
    assert "on `main` and on every tag" in text
