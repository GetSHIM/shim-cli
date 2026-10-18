"""Claude Code tool inputs through the installed hook, by where they go.

Each run is the hook as Claude Code starts it, in a fresh home and temporary
directory with no settings file, so what it answers is the shipped default.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMMAND = (sys.executable, "-I", "-B", "-m", "shim_cli.hook", "claude")
DRIVE = "/Users/dev/Library/CloudStorage/GoogleDrive-jane.doe@example.com/My Drive"
UNROUTED = (
    " shim cannot tell where this tool sends its input, so it refuses the call "
    "rather than rewrite it. Leave these values out, or ask the user how to "
    "continue."
)


def _hook(tmp_path: Path, event: str, tool: str, tool_input: dict, **extra):
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    home.mkdir()
    temporary.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SHIM_", "XDG_", "CLAUDE_"))
    }
    environment.update(HOME=str(home), TMPDIR=str(temporary))
    raw = json.dumps(
        {
            "session_id": "u04",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/work",
            "permission_mode": "default",
            "hook_event_name": event,
            "tool_name": tool,
            "tool_input": tool_input,
            **extra,
        }
    ).encode()
    result = subprocess.run(
        COMMAND,
        input=raw,
        capture_output=True,
        cwd=ROOT,
        env=environment,
        check=False,
        timeout=60,
    )
    assert (result.returncode, result.stderr) == (0, b"")
    records = [
        json.loads(line)
        for path in temporary.rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    return (json.loads(result.stdout) if result.stdout else None), records


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    (
        ("Read", {"file_path": f"{DRIVE}/proj/plan.txt"}),
        ("Glob", {"pattern": "**/*.py", "path": f"{DRIVE}/proj"}),
        ("Grep", {"pattern": "TODO", "path": f"{DRIVE}/proj"}),
    ),
)
def test_a_path_in_a_google_drive_folder_reaches_a_local_tool_unchanged(
    tmp_path: Path, tool: str, tool_input: dict
) -> None:
    output, [record] = _hook(tmp_path, "PreToolUse", tool, tool_input)

    assert output == {
        "systemMessage": f"shim: found EMAIL (1) in {tool}. Not modified."
    }
    assert (record["direction"], record["action"]) == ("local-tool", "report")
    assert "jane.doe" not in json.dumps(record)


@pytest.mark.parametrize(
    ("tool", "tool_input", "found"),
    (
        (
            "Monitor",
            {"command": "tail -f app.log | grep --line-buffered jane.doe@example.com"},
            "EMAIL (1)",
        ),
        (
            "mcp__ide__executeCode",
            {"code": "boto3.client('s3', aws_access_key_id='AKIAIOSFODNN7EXAMPLE')"},
            "SECRET (1)",
        ),
    ),
)
def test_a_command_another_tool_runs_is_reported_not_rewritten(
    tmp_path: Path, tool: str, tool_input: dict, found: str
) -> None:
    output, [record] = _hook(tmp_path, "PreToolUse", tool, tool_input)

    assert output == {"systemMessage": f"shim: found {found} in {tool}. Not modified."}
    assert (record["direction"], record["action"]) == ("executable-text", "report")


def test_an_answer_the_user_typed_reaches_the_model_unmasked(tmp_path: Path) -> None:
    question = "Which address should I set as git user.email?"
    questions = [{"question": question, "header": "Email", "multiSelect": False}]

    output, [record] = _hook(
        tmp_path,
        "PostToolUse",
        "AskUserQuestion",
        {"questions": questions},
        tool_response={
            "questions": questions,
            "answers": {question: "jane.doe@example.com"},
        },
    )

    assert output == {
        "systemMessage": "shim: found EMAIL (1) in AskUserQuestion. Not modified."
    }
    assert (record["direction"], record["action"]) == ("local-tool", "report")


def test_an_mcp_write_carrying_a_key_is_refused_under_the_shipped_default(
    tmp_path: Path,
) -> None:
    output, [record] = _hook(
        tmp_path,
        "PreToolUse",
        "mcp__filesystem__write_file",
        {"path": "/work/.env", "content": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"},
    )

    assert output == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "shim: blocked SECRET (1) in mcp__filesystem__write_file." + UNROUTED
            ),
        }
    }
    assert (record["direction"], record["mode"], record["action"]) == (
        "outbound",
        "enforce",
        "deny",
    )


def test_a_masked_fetch_tells_the_user(tmp_path: Path) -> None:
    output, [record] = _hook(
        tmp_path,
        "PreToolUse",
        "WebFetch",
        {"url": "https://example.com/?email=jane.doe@example.com", "prompt": "p"},
    )

    assert output["systemMessage"] == "shim: masked EMAIL (1) in WebFetch."
    assert "jane.doe" not in json.dumps(output["hookSpecificOutput"]["updatedInput"])
    assert "permissionDecision" not in output["hookSpecificOutput"]
    assert (record["direction"], record["action"]) == ("outbound", "mask")


PACKAGE = '{\n  "name": "widgets",\n  "version": "1.0.0",\n  "scripts": {"test": "vitest run"}\n}\n'


def test_a_file_cat_prints_is_handed_back_byte_for_byte(tmp_path: Path) -> None:
    output, [record] = _hook(
        tmp_path,
        "PostToolUse",
        "Bash",
        {"command": "cat package.json"},
        tool_response={"stdout": PACKAGE, "stderr": "", "interrupted": False},
    )

    assert output is None
    assert (record["action"], record["transforms"]) == ("allow", [])
