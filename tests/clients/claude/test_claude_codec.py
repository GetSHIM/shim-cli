from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from shim_cli.clients.claude.hook import block_output, error_output, parse_input
from shim_cli.clients.claude.tool_events import (
    MAX_INPUT_BYTES,
    TOOL_EVENTS,
    coverage,
    post_tool_use,
    post_tool_use_failure,
    pre_tool_use,
)
from shim_cli.events.pipeline import (
    INCOMPLETE_MESSAGE,
    REPORT_ONLY,
    _decide,
    _message,
)
from shim_cli.guard.entities import ENTITY_TYPES
from shim_cli.policy import ALLOW, DENY, MASK, REPORT
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
        (
            "PostToolUseFailure",
            b'{"hook_event_name":"PostToolUseFailure","tool_name":"Bash","error":5}',
        ),
        (
            "PostToolUseFailure",
            b'{"hook_event_name":"PostToolUse","tool_name":"Bash","error":"x"}',
        ),
    ),
)
def test_claude_tool_codec_rejects_malformed_wrong_or_oversized_events(
    event: str, raw: bytes
) -> None:
    with pytest.raises(ValueError):
        TOOL_EVENTS[event].decode(raw)


MASKED = "shim: masked EMAIL (1) in Read."


def test_a_masked_result_speaks_to_the_model_not_the_user() -> None:
    assert post_tool_use(
        MASK, {"content": "<EMAIL_1>"}, MASKED, "EMAIL (1)", "Read"
    ) == (
        b'{"hookSpecificOutput":{"hookEventName":"PostToolUse",'
        b'"updatedToolOutput":{"content":"<EMAIL_1>"},'
        b'"additionalContext":"shim: masked EMAIL (1) in Read. Placeholders such '
        b"as <EMAIL_1> stand for real values in the source; the source does not "
        b'contain placeholders."}}'
    )


def test_a_report_and_an_incomplete_mask_still_speak_to_the_user() -> None:
    incomplete = f"{MASKED} {INCOMPLETE_MESSAGE}"

    assert post_tool_use(REPORT, {}, MASKED, "EMAIL (1)", "Read") == (
        b'{"systemMessage":"shim: masked EMAIL (1) in Read."}'
    )
    assert json.loads(post_tool_use(MASK, {}, incomplete, "EMAIL (1)", "Read")) == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": {}},
        "systemMessage": incomplete,
    }
    assert json.loads(post_tool_use(MASK, {}, "", "", "Read")) == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": {}}
    }


def test_a_masked_argument_carries_no_permission_decision() -> None:
    assert (
        pre_tool_use(MASK, {}, MASKED, "EMAIL (1)", "Read")
        == pre_tool_use(MASK, {}, "", "", "Read")
        == b'{"hookSpecificOutput":{"hookEventName":"PreToolUse","updatedInput":{}}}'
    )


def test_the_model_context_is_bounded_at_every_type_and_the_longest_label() -> None:
    counts = tuple(sorted((entity, MAX_INPUT_BYTES) for entity in ENTITY_TYPES))
    label = "mcp__" + "x" * (MAX_DISPLAY_LABEL_CHARS - 5)

    output = json.loads(
        post_tool_use(MASK, {}, _message(label, counts, MASK), "", label)
    )
    context = output["hookSpecificOutput"]["additionalContext"]

    assert len(ENTITY_TYPES) == 12
    assert len(context) == 480


FAILED = (
    b'{"systemMessage":"shim: found SECRET (4), DB_URI (1) in a failed Bash. '
    b"Claude Code does not let this output be masked; the model has these "
    b'values.","hookSpecificOutput":{"hookEventName":"PostToolUseFailure",'
    b'"additionalContext":"shim: the output of this failed Bash contained '
    b"SECRET (4), DB_URI (1). Do not repeat these values in replies, files or "
    b'commands."}}'
)


def test_a_failed_call_with_findings_tells_the_user_and_the_model() -> None:
    message = _message("Bash", (("SECRET", 4), ("DB_URI", 1)), REPORT)

    output = post_tool_use_failure(
        REPORT, "x", message, "SECRET (4), DB_URI (1)", "Bash"
    )

    assert output == FAILED


def test_an_incomplete_failed_call_says_so_after_what_it_found() -> None:
    message = f"shim: found SECRET (1) in Bash. Not modified. {INCOMPLETE_MESSAGE}"

    output = json.loads(
        post_tool_use_failure(REPORT, "x", message, "SECRET (1)", "Bash")
    )

    assert output["systemMessage"].endswith(
        f"the model has these values. {INCOMPLETE_MESSAGE}"
    )
    assert "SECRET (1)" in output["hookSpecificOutput"]["additionalContext"]


def test_a_failed_call_that_could_not_be_inspected_only_tells_the_user() -> None:
    assert post_tool_use_failure(REPORT, "x", INCOMPLETE_MESSAGE, "", "Bash") == (
        b'{"systemMessage":"shim: inspection incomplete; uninspected content was '
        b'not modified."}'
    )


def test_a_clean_failed_call_says_nothing() -> None:
    assert post_tool_use_failure(ALLOW, "x", "", "", "Bash") == b""


@pytest.mark.parametrize("action", (MASK, DENY))
def test_a_failed_call_is_never_masked_or_denied(action: str) -> None:
    with pytest.raises(ValueError):
        post_tool_use_failure(action, "x", "m", "SECRET (1)", "Bash")


def test_a_failed_call_is_report_only_and_caps_enforce_to_a_report() -> None:
    entry = TOOL_EVENTS["PostToolUseFailure"]

    assert entry.root == "error"
    assert entry.power == REPORT_ONLY
    assert _decide(entry, "inbound", "enforce") == REPORT


def test_coverage_says_a_failed_call_cannot_be_masked() -> None:
    rows = {row["event"]: row for row in coverage()}

    assert rows["PostToolUseFailure"]["sees"] == "error"
    assert rows["PostToolUseFailure"]["can_mask"] is False
    assert rows["PostToolUse"]["can_mask"] is True
    assert rows["PreToolUse"]["can_mask"] is True
