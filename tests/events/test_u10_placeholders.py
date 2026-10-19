"""Placeholders shim issues: never one the source already holds (F081), and
never written back over the value it stands for (D3)."""

from __future__ import annotations

import json

import pytest

from shim_cli.clients.claude.tool_events import TOOL_EVENTS
from shim_cli.clients.vscode.tool_events import TOOL_EVENTS as VSCODE_EVENTS
from shim_cli.events import payload
from shim_cli.events.pipeline import process
from shim_cli.guard import evaluate
from shim_cli.policy import ALLOW, DENY, ENFORCE, MASK, OBSERVE

ADDRESS = "jane.doe@example.com"


def test_a_literal_placeholder_in_one_field_lifts_the_numbers_of_every_field() -> None:
    result = payload.inspect(
        {"doc": "Example output: <EMAIL_1> and <SECRET_4:12>", "owner": ADDRESS},
        evaluate,
    )

    assert result.value == {
        "doc": "Example output: <EMAIL_1> and <SECRET_4:12>",
        "owner": "<EMAIL_2>",
    }
    assert result.issued == (("EMAIL", 2, 2),)
    assert result.literal is True


def test_without_a_literal_placeholder_numbers_start_at_one() -> None:
    result = payload.inspect({"a": ADDRESS, "b": "bob@example.com"}, evaluate)

    assert result.value == {"a": "<EMAIL_1>", "b": "<EMAIL_2>"}
    assert result.issued == (("EMAIL", 1, 2),)
    assert result.literal is False


def test_a_prompt_numbers_above_its_own_literal_placeholders() -> None:
    decision = evaluate(f"was <EMAIL_7>, now {ADDRESS}")

    assert decision.redacted_text == "was <EMAIL_7>, now <EMAIL_8>"


def test_placeholder_shaped_text_of_no_type_shim_uses_does_not_lift_numbers() -> None:
    decision = evaluate(f"build <ADMIN_2024> by {ADDRESS}, <EMAIL_99999999>")

    assert decision.redacted_text == "build <ADMIN_2024> by <EMAIL_1>, <EMAIL_99999999>"


PAGE = (
    "<!doctype html><html><head><style>"
    + "p { margin: 0 }\n" * 160
    + "</style></head><body><p>Docs say {literal} is a placeholder.</p>"
    f"<p>Contact {ADDRESS}</p></body></html>"
)


@pytest.mark.parametrize(
    "literal",
    ["&lt;EMAIL_1&gt;", "&#60;EMAIL_1&#62;", "&lt;EMAIL_1&gt;<b></b>"],
    ids=["named-entities", "numeric-entities", "next-to-a-tag"],
)
def test_a_placeholder_the_converted_page_shows_lifts_the_numbers(
    literal: str,
) -> None:
    result = payload.inspect(
        {"result": PAGE.replace("{literal}", literal)}, evaluate, ("html",), True
    )

    assert result.transforms == ("html",)
    assert result.value["result"].endswith(
        "Docs say <EMAIL_1> is a placeholder.\nContact <EMAIL_2>"
    )
    assert result.issued == (("EMAIL", 2, 2),)
    assert result.literal is True


def test_a_page_that_shows_a_placeholder_only_once_converted_stays_markup() -> None:
    # A tag splits it: only the converted page would show <EMAIL_1>, and the
    # numbers were set before conversion, so the page is not converted.
    page = PAGE.replace("{literal}", "&lt;EMAIL_<b>1</b>&gt;")

    result = payload.inspect({"result": page}, evaluate, ("html",), True)

    assert result.transforms == ()
    assert result.value["result"] == page.replace(ADDRESS, "<EMAIL_1>")
    assert result.issued == (("EMAIL", 1, 1),)


def test_an_entity_is_not_decoded_for_numbering_when_the_page_is_not_converted() -> (
    None
):
    result = payload.inspect(
        {"result": PAGE.replace("{literal}", "&lt;EMAIL_1&gt;")}, evaluate
    )

    assert result.value["result"].endswith(
        "Docs say &lt;EMAIL_1&gt; is a placeholder.</p><p>Contact <EMAIL_1></p></body></html>"
    )
    assert result.issued == (("EMAIL", 1, 1),)
    assert result.literal is False


@pytest.mark.parametrize("shape", ["<EMAIL_99999999>", "<ADMIN_2024>", "<SECRET_3:12>"])
def test_any_placeholder_shaped_text_withdraws_the_no_placeholders_claim(
    shape: str,
) -> None:
    result = payload.inspect({"doc": f"see {shape}", "owner": ADDRESS}, evaluate)

    assert result.value["doc"] == f"see {shape}"
    assert result.literal is True


def _call(tool: str, tool_input: dict, adapter=TOOL_EVENTS) -> bytes:
    document = {
        "session_id": "s",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }
    if adapter is VSCODE_EVENTS:
        document["timestamp"] = "2026-10-19T00:00:00Z"
    return json.dumps(document).encode()


def _process(raw: bytes, issued, adapter=TOOL_EVENTS, mode: str = ENFORCE, **extra):
    return process(
        adapter["PreToolUse"],
        raw,
        lambda direction, tool: mode,
        evaluate,
        issued=issued,
        **extra,
    )


def _reason(outcome) -> str:
    specific = json.loads(outcome.output)["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    return specific["permissionDecisionReason"]


def test_two_issued_placeholders_are_named_once_each_in_order() -> None:
    outcome = _process(
        _call(
            "Write",
            {
                "file_path": "/w/.env",
                "content": "A=<SECRET_2>\nB=<EMAIL_1>\nC=<SECRET_2>\nD=<PHONE_1:34>",
            },
        ),
        lambda: [("EMAIL", 1, 1), ("SECRET", 1, 3), ("PHONE", 1, 1)],
    )

    assert _reason(outcome) == (
        "shim: this Write call would write <SECRET_2>, <EMAIL_1>, <PHONE_1>, "
        "placeholders shim gave you in place of real values, instead of those "
        "values. Change the real values another way, or ask the user."
    )
    assert (outcome.record.action, outcome.record.note) == (DENY, "placeholder")


@pytest.mark.parametrize("mode", (OBSERVE, ENFORCE))
def test_an_unknown_tool_is_held_to_it_like_an_mcp_tool(mode: str) -> None:
    outcome = _process(
        _call("custom_writer", {"body": {"text": "x <EMAIL_1>"}}, VSCODE_EVENTS),
        lambda: [("EMAIL", 1, 1)],
        VSCODE_EVENTS,
        mode,
    )

    assert "<EMAIL_1>" in _reason(outcome)


def test_a_placeholder_in_a_shell_command_is_left_to_the_shell_rules() -> None:
    outcome = _process(
        _call("Bash", {"command": "echo '<EMAIL_1>' > owner.txt"}),
        lambda: [("EMAIL", 1, 1)],
    )

    assert (outcome.output, outcome.record.action) == (b"", ALLOW)


def test_the_session_is_read_only_for_a_write_with_placeholder_shaped_text() -> None:
    def unread():
        raise AssertionError("read the session for a write without placeholders")

    outcome = _process(_call("Write", {"file_path": "/w/a", "content": "x"}), unread)

    assert outcome.output == b""


def test_a_session_that_cannot_be_read_refuses_the_write() -> None:
    def broken():
        raise OSError("synthetic")

    outcome = _process(
        _call(
            "Edit", {"file_path": "/w/a", "old_string": "x", "new_string": "<EMAIL_3>"}
        ),
        broken,
    )

    assert _reason(outcome) == (
        "shim: could not check this Edit call for placeholders shim gave you "
        "(session record unreadable), so it was not allowed."
    )


def test_a_mask_issues_the_names_it_wrote() -> None:
    issued = []
    raw = json.dumps(
        {
            "session_id": "s",
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/w/.env"},
            "tool_response": {"file": {"content": f"A=<EMAIL_1>\nB={ADDRESS}\n"}},
        }
    ).encode()

    outcome = process(
        TOOL_EVENTS["PostToolUse"],
        raw,
        lambda direction, tool: ENFORCE,
        evaluate,
        issue=issued.append,
    )

    assert outcome.record.action == MASK
    assert issued == [(("EMAIL", 2, 2),)]


def test_a_mask_that_cannot_issue_its_names_is_not_handed_on() -> None:
    def broken(_names):
        raise OSError("synthetic")

    raw = json.dumps(
        {
            "session_id": "s",
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/w/.env"},
            "tool_response": {"file": {"content": f"B={ADDRESS}\n"}},
        }
    ).encode()

    with pytest.raises(OSError):
        process(
            TOOL_EVENTS["PostToolUse"],
            raw,
            lambda direction, tool: ENFORCE,
            evaluate,
            issue=broken,
        )


def test_every_field_a_write_tool_may_write_is_read_for_placeholders() -> None:
    outcome = _process(
        _call(
            "edit_notebook_file",
            {"filePath": "/w/n.ipynb", "newCode": ["import os", "KEY = '<SECRET_1>'"]},
            VSCODE_EVENTS,
        ),
        lambda: [("SECRET", 1, 1)],
        VSCODE_EVENTS,
    )

    assert "<SECRET_1>" in _reason(outcome)


def test_the_text_an_edit_replaces_and_its_explanation_are_not_written() -> None:
    edit = {
        "filePath": "/w/.env",
        "oldString": "KEY=<SECRET_1>",
        "newString": "KEY=",
        "explanation": "Clear <SECRET_1>",
    }

    outcome = _process(
        _call("replace_string_in_file", edit, VSCODE_EVENTS),
        lambda: [("SECRET", 1, 1)],
        VSCODE_EVENTS,
    )

    assert (outcome.output, outcome.record.action) == (b"", ALLOW)
