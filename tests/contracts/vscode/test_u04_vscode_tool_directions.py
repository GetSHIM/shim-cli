"""VS Code tool inputs through the installed hook, by VS Code's own names.

A user who asks for `executable-text` or `local-write` enforcement gets it for
VS Code's terminal and file tools, and a file read stays a report whatever
`outbound` says. Fresh home and temporary directory per run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "probe" / "vscode"
COMMAND = (sys.executable, "-I", "-B", "-m", "shim_cli.hook", "vscode")
KEY = "AKIAIOSFODNN7EXAMPLE"


def _hook(tmp_path: Path, settings: str, tool: str, tool_input: dict):
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    config = tmp_path / "config.toml"
    home.mkdir()
    temporary.mkdir()
    config.write_text(settings, encoding="utf-8")
    config.chmod(0o600)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SHIM_", "XDG_", "CLAUDE_"))
    }
    environment.update(HOME=str(home), TMPDIR=str(temporary), SHIM_CONFIG=str(config))
    # The captured call, with only the tool and its input changed.
    document = json.loads((FIXTURES / "PreToolUse-run_in_terminal-1.json").read_text())
    document.update(tool_name=tool, tool_input=tool_input)
    result = subprocess.run(
        COMMAND,
        input=json.dumps(document).encode(),
        capture_output=True,
        cwd=ROOT,
        env=environment,
        check=False,
        timeout=60,
    )
    assert (result.returncode, result.stderr) == (0, b"")
    [record] = [
        json.loads(line)
        for path in temporary.rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    return (json.loads(result.stdout) if result.stdout else None), record


def _denied(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


@pytest.mark.parametrize(
    ("tool", "tool_input", "direction"),
    (
        (
            "run_in_terminal",
            {"command": f"aws configure set aws_access_key_id {KEY}", "mode": "sync"},
            "executable-text",
        ),
        (
            "create_file",
            {"filePath": "/p/.env", "content": f"AWS_ACCESS_KEY_ID={KEY}\n"},
            "local-write",
        ),
        (
            "replace_string_in_file",
            {"filePath": "/p/.env", "oldString": "x", "newString": KEY},
            "local-write",
        ),
    ),
)
def test_enforce_asked_for_a_direction_denies_vs_codes_own_tools(
    tmp_path: Path, tool: str, tool_input: dict, direction: str
) -> None:
    output, record = _hook(
        tmp_path, f'[mode]\n{direction} = "enforce"\n', tool, tool_input
    )

    assert output == _denied(f"shim: blocked SECRET (1) in {tool}.")
    assert (record["direction"], record["action"]) == (direction, "deny")


def test_a_file_read_is_reported_even_where_outbound_is_enforced(
    tmp_path: Path,
) -> None:
    path = "/Users/dev/Library/CloudStorage/GoogleDrive-jane.doe@example.com/a.txt"

    output, record = _hook(
        tmp_path, '[mode]\noutbound = "enforce"\n', "read_file", {"filePath": path}
    )

    assert output == {
        "systemMessage": "shim: found EMAIL (1) in read_file. Not modified."
    }
    assert (record["direction"], record["action"]) == ("local-tool", "report")
