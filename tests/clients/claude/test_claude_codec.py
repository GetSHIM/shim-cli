from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from shim_cli.clients.claude.hook import block_output, error_output, parse_input
from shim_cli.clients.claude.tool_events import (
    MAX_INPUT_BYTES,
    TOOL_EVENTS,
    post_tool_use,
    pre_tool_use,
)
from shim_cli.events.pipeline import INCOMPLETE_MESSAGE, _message
from shim_cli.guard.entities import ENTITY_TYPES
from shim_cli.policy import MASK, REPORT
from shim_cli.session.record import MAX_DISPLAY_LABEL_CHARS


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


MASKED = "shim: masked EMAIL (1) in Read."


def test_a_masked_result_speaks_to_the_model_not_the_user() -> None:
    assert post_tool_use(MASK, {"content": "<EMAIL_1>"}, MASKED) == (
        b'{"hookSpecificOutput":{"hookEventName":"PostToolUse",'
        b'"updatedToolOutput":{"content":"<EMAIL_1>"},'
        b'"additionalContext":"shim: masked EMAIL (1) in Read. Placeholders such '
        b"as <EMAIL_1> stand for real values in the source; the source does not "
        b'contain placeholders."}}'
    )


def test_a_report_and_an_incomplete_mask_still_speak_to_the_user() -> None:
    incomplete = f"{MASKED} {INCOMPLETE_MESSAGE}"

    assert post_tool_use(REPORT, {}, MASKED) == (
        b'{"systemMessage":"shim: masked EMAIL (1) in Read."}'
    )
    assert json.loads(post_tool_use(MASK, {}, incomplete)) == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": {}},
        "systemMessage": incomplete,
    }
    assert json.loads(post_tool_use(MASK, {}, "")) == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": {}}
    }


def test_a_masked_argument_output_is_unchanged() -> None:
    assert (
        pre_tool_use(MASK, {}, MASKED)
        == pre_tool_use(MASK, {}, "")
        == (
            b'{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
            b'"permissionDecision":"allow","updatedInput":{}}}'
        )
    )


def test_the_model_context_is_bounded_at_every_type_and_the_longest_label() -> None:
    counts = tuple(sorted((entity, MAX_INPUT_BYTES) for entity in ENTITY_TYPES))
    label = "mcp__" + "x" * (MAX_DISPLAY_LABEL_CHARS - 5)

    output = json.loads(post_tool_use(MASK, {}, _message(label, counts, MASK)))
    context = output["hookSpecificOutput"]["additionalContext"]

    assert len(ENTITY_TYPES) == 12
    assert len(context) == 480
