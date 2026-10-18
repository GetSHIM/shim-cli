from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

import pytest

FINDING_SCHEMA = Path(__file__).resolve().parents[1] / "docs/schemas/finding-v1.json"
# No text of 1 MB or less makes the detector give up by itself since U+FDFA is
# cut where it stops fitting, so a test that needs a refusal plants one: the
# recogniser fails on any piece holding this line, and the detector refuses
# that piece with its own error.
UNANALYSABLE = "a field the detector cannot analyse"


@pytest.fixture
def valid_findings():
    """A command's `findings`, each validated against the vendored schema v1."""
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(
        json.loads(FINDING_SCHEMA.read_text(encoding="utf-8")),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )

    def check(document: dict) -> list:
        for finding in document["findings"]:
            validator.validate(finding)
        return document["findings"]

    return check


@pytest.fixture(autouse=True)
def _isolated_roots(monkeypatch, tmp_path):
    root = tmp_path / "shim-roots"
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    temporary = root / "tmp"
    temporary.mkdir(parents=True)
    # gettempdir() caches its first answer; the spool and redaction files follow it.
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    monkeypatch.delenv("SHIM_CONFIG", raising=False)
    # A test that forgets `home=`, or an ambient client variable, would
    # otherwise reach the real client files.
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    # `shim watch` dials through the egress proxy its environment names; a
    # developer's proxy must not change what a test reaches.
    for name in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    return root


@pytest.fixture
def unanalysable(monkeypatch) -> str:
    """UNANALYSABLE, refused by the detector in this process."""
    analyze = importlib.import_module("shim_cli.guard.analyze")
    recognise = analyze.analyze_text

    def failing(text, *rest):
        if UNANALYSABLE in text:
            raise RuntimeError("synthetic recogniser failure")
        return recognise(text, *rest)

    monkeypatch.setattr(analyze, "analyze_text", failing)
    return UNANALYSABLE


@pytest.fixture
def unanalysable_hook() -> tuple:
    """The Claude Code hook as a subprocess whose detector refuses UNANALYSABLE."""
    code = (
        "import importlib, sys\n"
        "analyze = importlib.import_module('shim_cli.guard.analyze')\n"
        "recognise = analyze.analyze_text\n"
        "def failing(text, *rest):\n"
        f"    if {UNANALYSABLE!r} in text:\n"
        "        raise RuntimeError('synthetic recogniser failure')\n"
        "    return recognise(text, *rest)\n"
        "analyze.analyze_text = failing\n"
        "from shim_cli import hook\n"
        "sys.argv.append('claude')\n"
        "hook.main()\n"
    )
    return (sys.executable, "-I", "-B", "-c", code)
