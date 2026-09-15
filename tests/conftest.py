from __future__ import annotations

import tempfile

import pytest


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
    return root
