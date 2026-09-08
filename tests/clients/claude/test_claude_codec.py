from __future__ import annotations

from dataclasses import dataclass

import pytest

from shim_cli.clients.claude.hook import block_output, error_output, parse_input
from shim_cli.clients.claude.tool_events import MAX_INPUT_BYTES, TOOL_EVENTS


@dataclass(frozen=True)
class Decision:
    blocked: bool
    counts: tuple[tuple[str, int], ...] = ()


def test_claude_code_prompt_contract() -> None:
    raw = (
        b'{"session_id":"session","hook_event_name":"UserPromptSubmit",'
        b'"cwd":"/workspace","prompt":"hello \\ud83d\\udc4b"}'
    )
    assert parse_input(raw) == "hello 👋"
    assert block_output(Decision(False)) == b""
    assert block_output(Decision(True, (("EMAIL", 1),)), "/tmp/shim-redacted.txt") == (
        b'{"decision":"block","reason":"shim blocked this prompt: '
        b"EMAIL (1).\\nCopy and paste this as your next prompt:\\n"
        b'Read this file and use its contents as my prompt: /tmp/shim-redacted.txt",'
        b'"suppressOriginalPrompt":true}'
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b'{"hook_event_name":"Stop","prompt":"hello"}',
        b'{"hook_event_name":"UserPromptSubmit"}',
        b'{"hook_event_name":"UserPromptSubmit","prompt":false}',
        b'{"hook_event_name":"UserPromptSubmit","prompt":"a","prompt":"b"}',
        b'{"hook_event_name":"UserPromptSubmit","prompt":"a","number":1e999}',
        b'{"hook_event_name":"UserPromptSubmit","prompt":"\\ud800"}',
        b"\xff",
    ],
)
def test_claude_code_codec_rejects_hostile_payloads(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_input(raw)


def test_claude_code_error_is_a_native_generic_block() -> None:
    assert error_output() == (
        b'{"decision":"block","reason":"shim could not inspect this '
        b'prompt, so it was withheld. Run `shim doctor claude` for the reason.",'
        b'"suppressOriginalPrompt":true}'
    )


@pytest.mark.parametrize(
    ("event", "raw"),
    (
        ("PreToolUse", b"[]"),
        (
            "PreToolUse",
            b'{"hook_event_name":"PostToolUse","tool_name":"Read"}',
        ),
        ("PostToolUse", b" " * (MAX_INPUT_BYTES + 1)),
    ),
)
def test_claude_tool_codec_rejects_malformed_wrong_or_oversized_events(
    event: str, raw: bytes
) -> None:
    with pytest.raises(ValueError):
        TOOL_EVENTS[event].decode(raw)
