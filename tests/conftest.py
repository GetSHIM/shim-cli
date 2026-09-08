from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_roots(monkeypatch, tmp_path):
    root = tmp_path / "shim-roots"
    monkeypatch.delenv("SHIM_GUARD_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "config"))
    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", str(root / "session"))
    monkeypatch.delenv("SHIM_GUARD_CONFIG", raising=False)
    # A test that forgets `home=`, or an ambient client variable, would
    # otherwise reach the real client files.
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    return root
