from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

FIXED_MARKERS = (
    "/private/tmp/claude-",
    ".vscode/extensions/anthropic",
    "Library/Application Support",
    "SHIM_fullstack",
)

WIRE_PATTERNS = (
    "wrkspc_01",
    "anthropic-organization-id",
)

_GENERIC_HOME_NAMES = {"root", "runner", "user", "home", "ubuntu", "admin", "build"}

EXEMPT = {
    "tests/contracts/test_no_local_leakage.py",
    "tests/probe/test_probe_fixtures.py",
}


def _tracked() -> list[str]:
    result = subprocess.run(
        ("git", "ls-files"),
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        pytest.skip("not a git checkout", allow_module_level=True)
    return [line for line in result.stdout.decode().splitlines() if line]


def _home() -> Path | None:
    """The real home, read once at import.

    `tests/conftest.py` points `HOME` at a temporary directory for every test,
    so `Path.home()` inside a test is not the home these markers were built
    from. The self-checks below have to plant the same one, or they assert
    that a temporary path matches a marker derived from a different path —
    which passes only where the username happens to appear in `tmp_path`.
    """
    try:
        return Path.home()
    except RuntimeError:
        return None


HOME = _home()


def _markers() -> list[str]:
    found = list(FIXED_MARKERS) + list(WIRE_PATTERNS)
    home = HOME
    if home is None:
        return found
    found.append(str(home))
    name = home.name
    if len(name) >= 4 and name.lower() not in _GENERIC_HOME_NAMES:
        found.append(name)
    for variable in ("USER", "LOGNAME"):
        value = os.environ.get(variable, "")
        if len(value) >= 4 and value.lower() not in _GENERIC_HOME_NAMES:
            found.append(value)
    return found


MARKERS = _markers()
FILES = [path for path in _tracked() if path not in EXEMPT]


def test_there_is_something_to_check() -> None:
    assert len(FILES) > 50, "the tracked-file listing looks wrong"
    assert MARKERS


@pytest.mark.parametrize("relative", FILES, ids=lambda path: path)
def test_no_committed_file_names_the_machine_it_was_written_on(
    relative: str,
) -> None:
    raw = (ROOT / relative).read_bytes()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        # A screenshot is committed evidence like any other file, and carries
        # metadata a text decode would never see. Match the markers as bytes
        # rather than skipping the file.
        content = raw.decode("latin-1")

    hits = sorted({marker for marker in MARKERS if marker in content})

    assert not hits, (
        f"{relative} contains {hits}. This repository is public and its "
        "examples come from real sessions; replace the value with a synthetic "
        "one rather than trimming it."
    )


@pytest.mark.skipif(HOME is None, reason="no home to plant")
def test_the_guard_reads_a_marker_planted_in_a_binary_file(tmp_path: Path) -> None:
    """A PNG carries text in its metadata chunks; a text decode would skip it."""
    planted = tmp_path / "shot.png"
    planted.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0btEXtComment\x00"
        + f"captured in {HOME}/repo".encode()
        + b"\xff\xfe\x00"
    )

    content = planted.read_bytes().decode("latin-1")

    assert [marker for marker in MARKERS if marker in content]


@pytest.mark.skipif(HOME is None, reason="no home to plant")
def test_the_guard_catches_a_planted_marker() -> None:
    planted = f"see {HOME}/notes.md for details"

    assert any(marker in planted for marker in MARKERS)


@pytest.mark.skipif(sys.platform == "win32", reason="posix paths")
def test_a_synthetic_home_is_still_allowed() -> None:
    synthetic = "Read /Users/alice/.ssh/id_rsa, then continue"

    assert not [marker for marker in MARKERS if marker in synthetic]
