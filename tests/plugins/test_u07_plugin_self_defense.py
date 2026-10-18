"""shim's marketplace plugin cannot be switched off through the agent's write
tools, and its archive still lets an ordinary write through.

Each run is the plugin's launcher as Claude Code starts it for a tool call,
with no package hook on PATH, so it runs the archive built from this tree, in
a fresh home and temporary directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from functools import partial
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "plugins" / "shim-cli" / "hooks"
BUILDER = ROOT / "scripts" / "build_zipapp.py"
SETTINGS_REFUSAL = (
    "shim: refused this change to {name}, which holds shim's settings or hooks; "
    "an agent that can change it can turn shim off. To change it, edit it "
    "yourself in your own terminal."
)


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("zipapp") / "shim.pyz"
    subprocess.run(
        (sys.executable, str(BUILDER), "--output", str(target)),
        capture_output=True,
        check=True,
        env=os.environ | {"TZ": "UTC"},
        preexec_fn=partial(os.umask, 0o022),
        timeout=300,
    )
    return target


def _plugin(tmp_path: Path, archive: Path) -> Path:
    """The plugin as Claude Code installs it, with this tree's archive."""
    root = tmp_path / "plugin"
    shutil.copytree(HOOKS, root / "hooks")
    (root / "bin").mkdir()
    shutil.copy2(archive, root / "bin" / "shim.pyz")
    return root


def _hook(tmp_path: Path, root: Path, tool: str, tool_input: dict):
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    interpreters = tmp_path / "interpreters"
    for folder in (home, temporary, interpreters):
        folder.mkdir(exist_ok=True)
    # -S as well as the launcher's -I: no installed package may stand in for
    # a module the archive forgot.
    python = interpreters / f"python{sys.version_info.major}.{sys.version_info.minor}"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
    python.chmod(0o755)
    event = {
        "session_id": "u07",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": str(home),
        "permission_mode": "bypassPermissions",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }
    result = subprocess.run(
        (str(root / "hooks" / "run-shim"), "claude", "call"),
        input=json.dumps(event).encode(),
        capture_output=True,
        check=False,
        env={
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "PATH": str(interpreters),
            "CLAUDE_PLUGIN_ROOT": str(root),
        },
        timeout=60,
    )
    assert (result.returncode, result.stderr) == (0, b"")
    records = [
        json.loads(line)
        for path in temporary.rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    return result.stdout, records


def _denied(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def test_an_ordinary_write_passes_through_the_archive(
    tmp_path: Path, archive: Path
) -> None:
    root = _plugin(tmp_path, archive)

    stdout, [record] = _hook(
        tmp_path,
        root,
        "Write",
        {"file_path": str(tmp_path / "home" / "notes.md"), "content": "hello"},
    )

    assert stdout == b""
    assert (record["action"], record["note"]) == ("allow", "")


def test_the_archive_refuses_a_write_to_shim_s_settings(
    tmp_path: Path, archive: Path
) -> None:
    root = _plugin(tmp_path, archive)
    settings = tmp_path / "home" / ".config" / "shim" / "config.toml"

    stdout, [record] = _hook(
        tmp_path,
        root,
        "Write",
        {"file_path": str(settings), "content": "enabled_entities = []\n"},
    )

    assert json.loads(stdout) == _denied(SETTINGS_REFUSAL.format(name="config.toml"))
    assert (record["action"], record["note"]) == ("deny", "self-protection")


def test_the_claude_settings_switching_the_plugin_on_are_refused(
    tmp_path: Path, archive: Path
) -> None:
    root = _plugin(tmp_path, archive)
    settings = tmp_path / "home" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    content = b'{"enabledPlugins": {"shim-cli@shim-cli": true}}\n'
    settings.write_bytes(content)

    stdout, [record] = _hook(
        tmp_path,
        root,
        "Edit",
        {"file_path": str(settings), "old_string": "true", "new_string": "false"},
    )

    assert json.loads(stdout) == _denied(SETTINGS_REFUSAL.format(name="settings.json"))
    assert record["action"] == "deny"
    assert settings.read_bytes() == content


@pytest.mark.parametrize(
    "inside",
    ("hooks/claude.json", "hooks/run-shim", "bin/shim.pyz", "hooks/hooks.json"),
)
def test_a_write_inside_the_plugin_folder_is_refused(
    tmp_path: Path, archive: Path, inside: str
) -> None:
    root = _plugin(tmp_path, archive)
    target = root / inside
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

    stdout, [record] = _hook(
        tmp_path, root, "Write", {"file_path": str(target), "content": "exit 0\n"}
    )

    assert json.loads(stdout) == _denied(SETTINGS_REFUSAL.format(name=target.name))
    assert (record["action"], record["note"]) == ("deny", "self-protection")
    assert {
        path: path.read_bytes() for path in root.rglob("*") if path.is_file()
    } == before
