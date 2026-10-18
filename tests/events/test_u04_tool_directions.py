"""Each tool input is handled by where it goes, under the shipped defaults.

A local tool's input is an address or a question: rewriting it breaks the
call and protects nothing, so it is reported. A command is never rewritten.
An MCP or unknown tool may send its input anywhere, so it is refused under
enforce rather than handed placeholders.
"""

from __future__ import annotations

import json

import pytest

from shim_cli.clients.claude.tool_events import TOOL_EVENTS
from shim_cli.config import load_policy
from shim_cli.events.pipeline import process
from shim_cli.guard import evaluate
from shim_cli.policy import (
    ALLOW,
    DENY,
    ENFORCE,
    EXECUTABLE_TEXT,
    INBOUND,
    MASK,
    OBSERVE,
    OUTBOUND,
    REPORT,
    WARN,
)

# The settings key a user writes, spelled out so a rename is a failing test.
LOCAL_TOOL = "local-tool"
DRIVE = "/Users/dev/Library/CloudStorage/GoogleDrive-jane.doe@example.com/My Drive"


def _raw(event: str, tool: str, tool_input: dict, response=None) -> bytes:
    document = {
        "session_id": "s",
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": tool_input,
    }
    if response is not None:
        document["tool_response"] = response
    return json.dumps(document).encode()


def _shipped(raw: bytes, modes: dict | None = None):
    """Through the pipeline as the hook runs it with no settings file."""
    shipped = load_policy()
    event = json.loads(raw)["hook_event_name"]
    modes = modes or {}

    def mode_for(direction: str, tool: str) -> str:
        return modes.get(direction) or shipped.mode_for(direction, tool, event)

    return process(TOOL_EVENTS[event], raw, mode_for, evaluate, shipped.diet)


def _json(outcome):
    return json.loads(outcome.output) if outcome.output else None


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    (
        ("Read", {"file_path": f"{DRIVE}/proj/plan.txt"}),
        ("Glob", {"pattern": "**/*.py", "path": f"{DRIVE}/proj"}),
        ("Grep", {"pattern": "TODO", "path": f"{DRIVE}/proj"}),
        ("LS", {"path": f"{DRIVE}/proj"}),
        ("NotebookRead", {"notebook_path": f"{DRIVE}/proj/a.ipynb"}),
    ),
)
@pytest.mark.parametrize(
    ("mode", "action"), ((OBSERVE, ALLOW), (WARN, REPORT), (ENFORCE, REPORT))
)
def test_a_local_tool_path_is_reported_never_rewritten(
    tool: str, tool_input: dict, mode: str, action: str
) -> None:
    outcome = _shipped(_raw("PreToolUse", tool, tool_input), {LOCAL_TOOL: mode})

    assert outcome.record.direction == LOCAL_TOOL
    assert outcome.record.action == action
    assert outcome.record.entities == (("EMAIL", 1),)
    expected = {"systemMessage": f"shim: found EMAIL (1) in {tool}. Not modified."}
    assert _json(outcome) == (expected if action == REPORT else None)


def test_a_local_tool_is_reported_by_default() -> None:
    outcome = _shipped(_raw("PreToolUse", "Read", {"file_path": f"{DRIVE}/a.txt"}))

    assert (outcome.record.mode, outcome.record.action) == (WARN, REPORT)


@pytest.mark.parametrize(
    ("tool", "tool_input", "entity"),
    (
        (
            "Monitor",
            {
                "command": "tail -f app.log | grep --line-buffered jane.doe@example.com",
                "description": "watch",
            },
            "EMAIL",
        ),
        (
            "mcp__ide__executeCode",
            {"code": "boto3.client('s3', aws_access_key_id='AKIAIOSFODNN7EXAMPLE')"},
            "SECRET",
        ),
        ("PowerShell", {"command": "Send-Mail -To jane.doe@example.com"}, "EMAIL"),
    ),
)
@pytest.mark.parametrize(
    ("mode", "action"), ((OBSERVE, ALLOW), (WARN, REPORT), (ENFORCE, DENY))
)
def test_a_command_run_by_another_tool_is_never_rewritten(
    tool: str, tool_input: dict, entity: str, mode: str, action: str
) -> None:
    outcome = _shipped(_raw("PreToolUse", tool, tool_input), {EXECUTABLE_TEXT: mode})

    assert outcome.record.direction == EXECUTABLE_TEXT
    assert outcome.record.action == action
    assert b"updatedInput" not in outcome.output


@pytest.mark.parametrize(
    "tool",
    ("mcp__filesystem__write_file", "SendMessage", "SomeToolANewerClientAdds"),
)
def test_an_mcp_or_unknown_tool_is_refused_by_default_not_handed_placeholders(
    tool: str,
) -> None:
    tool_input = {
        "path": "/work/.env",
        "content": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n",
    }

    outcome = _shipped(_raw("PreToolUse", tool, tool_input))

    assert outcome.record.direction == OUTBOUND
    assert (outcome.record.mode, outcome.record.action) == (ENFORCE, DENY)
    assert _json(outcome) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"shim: blocked SECRET (1) in {tool}. shim cannot tell where this "
                "tool sends its input, so it refuses the call rather than rewrite "
                "it. Leave these values out, or ask the user how to continue."
            ),
        }
    }


@pytest.mark.parametrize("tool", ("WebFetch", "WebSearch"))
def test_a_network_tool_is_still_masked_by_default(tool: str) -> None:
    outcome = _shipped(
        _raw("PreToolUse", tool, {"query": "who is jane.doe@example.com"})
    )

    assert (outcome.record.direction, outcome.record.action) == (OUTBOUND, MASK)
    assert _json(outcome) == {
        "systemMessage": f"shim: masked EMAIL (1) in {tool}.",
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {"query": "who is <EMAIL_1>"},
        },
    }


QUESTION = "Which address should I set as git user.email?"


def test_an_answer_the_user_typed_is_reported_not_masked() -> None:
    questions = [{"question": QUESTION, "header": "Email", "multiSelect": False}]
    raw = _raw(
        "PostToolUse",
        "AskUserQuestion",
        {"questions": questions},
        {"questions": questions, "answers": {QUESTION: "jane.doe@example.com"}},
    )

    outcome = _shipped(raw)

    assert outcome.record.direction == LOCAL_TOOL
    assert (outcome.record.mode, outcome.record.action) == (WARN, REPORT)
    assert _json(outcome) == {
        "systemMessage": "shim: found EMAIL (1) in AskUserQuestion. Not modified."
    }


def test_another_local_tool_result_is_still_masked() -> None:
    raw = _raw(
        "PostToolUse",
        "Grep",
        {"pattern": "owner"},
        {"mode": "content", "content": "owner = jane.doe@example.com"},
    )

    outcome = _shipped(raw)

    assert (outcome.record.direction, outcome.record.action) == (INBOUND, MASK)


PACKAGE = '{\n  "name": "widgets",\n  "version": "1.0.0",\n  "scripts": {"test": "vitest run"}\n}\n'


def test_a_file_printed_by_a_command_is_never_compacted() -> None:
    raw = _raw(
        "PostToolUse",
        "Bash",
        {"command": "cat package.json"},
        {"stdout": PACKAGE, "stderr": "", "interrupted": False},
    )

    outcome = _shipped(raw)

    assert (outcome.output, outcome.record.transforms) == (b"", ())


def test_an_mcp_read_is_compacted_and_the_model_is_told() -> None:
    raw = _raw(
        "PostToolUse",
        "mcp__fs__read_file",
        {"uri": "file:///w/package.json"},
        [{"type": "text", "text": PACKAGE}],
    )

    outcome = _shipped(raw)

    assert outcome.record.transforms == ("json",)
    assert _json(outcome) == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": [
                {
                    "type": "text",
                    "text": json.dumps(json.loads(PACKAGE), separators=(",", ":")),
                }
            ],
            "additionalContext": (
                "shim: reformatted this result to save space (json); it is not "
                "byte-for-byte what the tool returned."
            ),
        }
    }
