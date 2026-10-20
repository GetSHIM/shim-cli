"""D3 in VS Code: its edit tools are refused a placeholder shim issued.

VS Code never masks a tool result, but a refused prompt's redacted copy hands
the model placeholders all the same. Fresh home and temporary directory per
test, one session.
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


def _hook(tmp_path: Path):
    for name in ("home", "tmp"):
        (tmp_path / name).mkdir()
    config = tmp_path / "config.toml"
    config.write_text('[mode]\nuser-prompt = "enforce"\n', encoding="utf-8")
    config.chmod(0o600)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SHIM_", "XDG_", "CLAUDE_"))
    }
    environment.update(
        HOME=str(tmp_path / "home"),
        TMPDIR=str(tmp_path / "tmp"),
        SHIM_CONFIG=str(config),
    )

    def run(fixture: str, **changes) -> bytes:
        document = json.loads((FIXTURES / fixture).read_text())
        document.update(changes)
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
        return result.stdout

    stopped = json.loads(
        run("UserPromptSubmit-none-1.json", prompt="owner is jane.doe@example.com")
    )
    assert stopped["continue"] is False
    return lambda tool, tool_input: run(
        "PreToolUse-run_in_terminal-1.json", tool_name=tool, tool_input=tool_input
    )


PATCH = (
    "*** Begin Patch\n*** Update File: /probe/workspace/o.txt\n"
    "@@\n-owner = nobody\n+owner = <EMAIL_1>\n*** End Patch\n"
)
EDITS = (
    ("create_file", {"filePath": "/probe/workspace/o.txt", "content": "<EMAIL_1>"}),
    (
        "replace_string_in_file",
        {
            "filePath": "/probe/workspace/o.txt",
            "oldString": "x",
            "newString": "<EMAIL_1>",
        },
    ),
    (
        "multi_replace_string_in_file",
        {
            "replacements": [
                {
                    "filePath": "/probe/workspace/o.txt",
                    "oldString": "a",
                    "newString": "b",
                },
                {
                    "filePath": "/probe/workspace/o.txt",
                    "oldString": "x",
                    "newString": "<EMAIL_1>",
                },
            ]
        },
    ),
    (
        "insert_edit_into_file",
        {"filePath": "/probe/workspace/o.txt", "code": "<EMAIL_1>", "explanation": "e"},
    ),
    ("apply_patch", {"input": PATCH, "explanation": "e"}),
)


@pytest.mark.parametrize(("tool", "tool_input"), EDITS, ids=[tool for tool, _ in EDITS])
def test_a_vscode_edit_of_an_issued_placeholder_is_refused(
    tmp_path: Path, tool: str, tool_input: dict
) -> None:
    call = _hook(tmp_path)

    specific = json.loads(call(tool, tool_input))["hookSpecificOutput"]

    assert specific["permissionDecision"] == "deny"
    assert specific["permissionDecisionReason"].startswith(
        f"shim: this {tool} call would write <EMAIL_1>, a placeholder shim gave you"
    )


def test_a_patch_that_only_removes_a_placeholder_is_allowed(tmp_path: Path) -> None:
    call = _hook(tmp_path)
    patch = PATCH.replace(
        "-owner = nobody\n+owner = <EMAIL_1>", "-owner = <EMAIL_1>\n+owner ="
    )

    assert call("apply_patch", {"input": patch, "explanation": "e"}) == b""
