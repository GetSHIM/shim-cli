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
    _RESHAPED,
    REPORT_ONLY,
    UNINSPECTED,
    _decide,
    _message,
    _summary,
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


def test_a_report_speaks_to_the_user_and_a_silent_mask_to_no_one() -> None:
    assert post_tool_use(REPORT, {}, MASKED, "EMAIL (1)", "Read") == (
        b'{"systemMessage":"shim: masked EMAIL (1) in Read."}'
    )
    assert json.loads(post_tool_use(MASK, {}, "", "", "Read")) == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": {}}
    }


WITHHELD = "[withheld by shim]"
WITHHELD_CONTEXT = (
    "shim withheld this {tool} result because it could not be inspected "
    "({reason}). It is not empty; do not guess what it held, and ask the user "
    "how to continue."
)
# A withheld image or PDF is a text Read: as a file it is sent to the API,
# which refuses one that is not real on every later request of the session.
WITHHELD_READ = {
    "type": "text",
    "file": {
        "filePath": "",
        "content": WITHHELD,
        "numLines": 0,
        "startLine": 0,
        "totalLines": 0,
    },
}


@pytest.mark.parametrize(
    ("tool", "result", "replacement"),
    (
        (
            "Read",
            {
                "type": "text",
                "file": {
                    "filePath": "/work/service/.env",
                    "content": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n",
                    "numLines": 1,
                    "startLine": 1,
                    "totalLines": 1,
                    "truncatedByTokenCap": False,
                },
            },
            {
                "type": "text",
                "file": {
                    "filePath": WITHHELD,
                    "content": WITHHELD,
                    "numLines": 0,
                    "startLine": 0,
                    "totalLines": 0,
                    "truncatedByTokenCap": False,
                },
            },
        ),
        (
            "Bash",
            {"stdout": "key=AKIAIOSFODNN7EXAMPLE", "stderr": "", "interrupted": False},
            {"stdout": WITHHELD, "stderr": "", "interrupted": False},
        ),
        (
            "Grep",
            {
                "mode": "files_with_matches",
                "filenames": ["/work/service/.env", "/work/a"],
                "numFiles": 2,
            },
            {"mode": "files_with_matches", "filenames": [WITHHELD], "numFiles": 0},
        ),
        (
            "Read",
            {"type": "jane.doe@example.com", "file": {"filePath": "a"}},
            {"type": WITHHELD, "file": {"filePath": ""}},
        ),
        (
            # Claude Code keeps a cell's text under `source`, and types the
            # cells as anything, so no word of it is needed to pass.
            "Read",
            {
                "type": "notebook",
                "file": {
                    "filePath": "/work/creds.ipynb",
                    "cells": [
                        {
                            "cellType": "markdown",
                            "source": "Jane-Doe-Personal-Token/hunter",
                            "cell_id": "c1",
                        },
                        {"cellType": "code", "source": "hunter", "cell_id": "c2"},
                    ],
                },
            },
            {
                "type": "notebook",
                "file": {
                    "filePath": "",
                    "cells": [{"cellType": "", "source": WITHHELD, "cell_id": ""}],
                },
            },
        ),
        (
            "Read",
            {
                "type": "file_unchanged",
                "file": {"filePath": "/work/CLAUDE.md"},
                "source": "seeded",
            },
            {"type": "file_unchanged", "file": {"filePath": ""}, "source": "seeded"},
        ),
        (
            "Read",
            {"type": "file_unchanged", "file": {"filePath": "a"}, "source": "hunter"},
            {"type": "file_unchanged", "file": {"filePath": ""}, "source": ""},
        ),
        (
            "Agent",
            {
                "status": "completed",
                "agentId": "a1",
                "content": [{"type": "text", "text": "key AKIAIOSFODNN7EXAMPLE"}],
                "totalDurationMs": 5,
                "handback": "send",
            },
            {
                "status": "completed",
                "agentId": "",
                "content": [{"type": "text", "text": WITHHELD}],
                "totalDurationMs": 0,
                "handback": "send",
            },
        ),
        (
            # Each match goes to the API as a tool reference by name; an empty
            # or unknown name is refused on every later request of the session.
            "ToolSearch",
            {
                "matches": ["WebFetch", "mcp__claude_ai_Notion__notion-search"],
                "query": "jane.doe@example.com",
                "total_deferred_tools": 16,
            },
            {
                "matches": ["WebFetch", "mcp__claude_ai_Notion__notion-search"],
                "query": WITHHELD,
                "total_deferred_tools": 0,
            },
        ),
        (
            # Sent to the API as an image whatever its data; a blank one is not.
            "Read",
            {
                "type": "image",
                "file": {
                    "base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4",
                    "type": "image/png",
                    "originalSize": 70,
                },
            },
            WITHHELD_READ,
        ),
        (
            "Read",
            {
                "type": "pdf",
                "file": {
                    "filePath": "/work/hunter.pdf",
                    "base64": "JVBERi0xLjQKMSAwIG9iago8PCAvVHlwZSAvQ2F0YWxvZyA+Pgo=",
                    "originalSize": 599,
                },
            },
            WITHHELD_READ,
        ),
        (
            "Read",
            {
                "type": "parts",
                "file": {
                    "filePath": "/work/hunter.pdf",
                    "originalSize": 599,
                    "count": 1,
                    "outputDir": "/tmp/hunter-pages",
                },
                "pages": [
                    {"base64": "iVBORw0KGgoAAAANSUhEUg==", "mediaType": "image/png"}
                ],
            },
            WITHHELD_READ,
        ),
    ),
    ids=(
        "read",
        "bash",
        "grep",
        "kind-key-holding-data",
        "notebook-cell-text",
        "unchanged-seeded",
        "source-holding-a-word",
        "agent-handback",
        "tool-search-names",
        "read-image",
        "read-pdf",
        "read-pdf-pages",
    ),
)
def test_an_uninspected_built_in_result_is_withheld_in_its_own_shape(
    tool: str, result: dict, replacement: dict
) -> None:
    """Claude Code 2.1.286 checks a built-in tool's replacement against the
    tool's output schema and, if it fails, sends the original. So the shape
    stays — keys, one item per list, zeroed numbers, the words those schemas
    pin — and no text of the result does."""
    output = post_tool_use(UNINSPECTED, result, "deadline", "SECRET (1)", tool, tool)

    assert b"AKIAIOSFODNN7EXAMPLE" not in output
    assert b"jane.doe" not in output
    assert b"hunter" not in output
    assert json.loads(output) == {
        "systemMessage": (
            f"shim: withheld this {tool} result; it could not be inspected (deadline)."
        ),
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": replacement,
            "additionalContext": WITHHELD_CONTEXT.format(tool=tool, reason="deadline"),
        },
    }


@pytest.mark.parametrize(
    "result",
    (
        [{"type": "text", "text": "AKIAIOSFODNN7EXAMPLE"}],
        {"jane.doe@example.com": {"role": "admin"}},
        "AKIAIOSFODNN7EXAMPLE",
        None,
    ),
    ids=("blocks", "object-keyed-by-data", "text", "shape-unknown"),
)
@pytest.mark.parametrize(
    ("label", "name"),
    (
        ("mcp__crm__lookup", "mcp__crm__lookup"),
        # Too long to show, so labelled as unknown: still an MCP result.
        ("unknown tool", "mcp__" + "s" * 120 + "__lookup"),
        # A custom pattern that matches the name's own prefix leaves a label
        # that no longer starts with it.
        ("<ACME_1>__lookup", "mcp__acme__lookup"),
        # Claude Code always names the tool; a result without a name is
        # nobody's built-in result to keep the shape of.
        ("unknown tool", ""),
    ),
    ids=("named", "label-too-long", "label-redacted", "no-name"),
)
def test_an_uninspected_mcp_result_keeps_nothing_of_the_original(
    result, label: str, name: str
) -> None:
    output = post_tool_use(UNINSPECTED, result, "too large", "", label, name)

    assert b"AKIA" not in output
    assert b"jane.doe" not in output
    assert b"admin" not in output
    document = json.loads(output)
    assert document["hookSpecificOutput"]["updatedToolOutput"] == [
        {"type": "text", "text": WITHHELD}
    ]


def test_a_built_in_result_keeps_its_shape_whatever_its_label_says() -> None:
    """The label is what the user reads, and falls back to "unknown tool" when
    it cannot be checked; the tool's own name is what Claude Code goes by."""
    result = {"stdout": "key=AKIAIOSFODNN7EXAMPLE", "stderr": "", "interrupted": False}

    output = post_tool_use(
        UNINSPECTED, result, "analysis failed", "", "unknown tool", "Bash"
    )

    assert json.loads(output)["hookSpecificOutput"]["updatedToolOutput"] == {
        "stdout": WITHHELD,
        "stderr": "",
        "interrupted": False,
    }


def test_a_built_in_result_nested_past_the_recursion_limit_is_withheld() -> None:
    """No output schema is anywhere near this deep: past the depth inspection
    stops at, the rest goes, instead of the walk dying in a RecursionError."""
    import sys

    from shim_cli.events.payload import MAX_DEPTH

    result: object = "key=AKIAIOSFODNN7EXAMPLE"
    for _ in range(sys.getrecursionlimit() + 100):
        result = {"content": result}

    output = post_tool_use(UNINSPECTED, result, "too large", "", "WebFetch", "WebFetch")

    assert b"AKIA" not in output
    replaced = json.loads(output)["hookSpecificOutput"]["updatedToolOutput"]
    depth = 0
    while isinstance(replaced, dict):
        replaced, depth = replaced["content"], depth + 1
    assert (replaced, depth) == ("", MAX_DEPTH + 1)


def test_an_uninspected_built_in_result_of_unknown_shape_is_reported() -> None:
    output = post_tool_use(UNINSPECTED, None, "too large", "", "Read", "Read")

    assert json.loads(output) == {
        "systemMessage": (
            "shim: could not inspect this Read output (too large); the model has "
            "it as it was."
        ),
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                "shim could not inspect this Read output (too large). Do not "
                "repeat values from it in replies, files or commands."
            ),
        },
    }


def test_an_uninspected_call_is_denied_with_its_reason() -> None:
    """The PreToolUse half of an incomplete inspection: nothing is rewritten,
    the call does not run, and the reason names only the category."""
    arguments = {"url": "https://example.com/?key=AKIAIOSFODNN7EXAMPLE"}

    output = pre_tool_use(UNINSPECTED, arguments, "deadline", "SECRET (1)", "WebFetch")

    assert output == (
        b'{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
        b'"permissionDecision":"deny","permissionDecisionReason":'
        b'"shim: could not inspect this WebFetch call (deadline), so it was not '
        b'allowed."}}'
    )


def test_a_masked_argument_tells_the_user_and_carries_no_permission_decision() -> None:
    assert pre_tool_use(MASK, {}, MASKED, "EMAIL (1)", "Read") == (
        b'{"systemMessage":"shim: masked EMAIL (1) in Read.",'
        b'"hookSpecificOutput":{"hookEventName":"PreToolUse","updatedInput":{}}}'
    )


def test_the_model_context_is_bounded_at_every_type_and_the_longest_label() -> None:
    counts = tuple(sorted((entity, MAX_INPUT_BYTES) for entity in ENTITY_TYPES))
    label = "mcp__" + "x" * (MAX_DISPLAY_LABEL_CHARS - 5)

    # As the pipeline sends it when every transform reshaped the result too.
    reshaped = _RESHAPED.format("json, html, whitespace")
    message = f"{reshaped} {_message(label, counts, MASK)}"

    output = json.loads(post_tool_use(MASK, {}, message, _summary(counts), label))
    context = output["hookSpecificOutput"]["additionalContext"]

    assert len(ENTITY_TYPES) == 13
    assert len(context) == len(reshaped) + 1 + 508


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
    output = json.loads(
        post_tool_use_failure(UNINSPECTED, "x", "deadline", "SECRET (1)", "Bash")
    )

    assert output["systemMessage"] == (
        "shim: could not inspect this Bash output (deadline); the model has it as "
        "it was. Before it stopped, shim found SECRET (1)."
    )
    assert output["hookSpecificOutput"]["additionalContext"] == (
        "shim could not inspect this Bash output (deadline). Do not repeat values "
        "from it in replies, files or commands."
    )


def test_a_failed_call_that_could_not_be_inspected_tells_the_user_and_model() -> None:
    assert post_tool_use_failure(UNINSPECTED, "x", "analysis failed", "", "Bash") == (
        b'{"systemMessage":"shim: could not inspect this Bash output (analysis '
        b'failed); the model has it as it was.","hookSpecificOutput":'
        b'{"hookEventName":"PostToolUseFailure","additionalContext":"shim could '
        b"not inspect this Bash output (analysis failed). Do not repeat values "
        b'from it in replies, files or commands."}}'
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
    assert _decide(entry, "inbound", "enforce", False) == REPORT


def test_coverage_says_a_failed_call_cannot_be_masked() -> None:
    rows = {row["event"]: row for row in coverage()}

    assert rows["PostToolUseFailure"]["sees"] == "error"
    assert rows["PostToolUseFailure"]["can_mask"] is False
    assert rows["PostToolUse"]["can_mask"] is True
    assert rows["PreToolUse"]["can_mask"] is True
