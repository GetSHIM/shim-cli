"""VS Code reports, and refuses only where refusing works.

Measured against VS Code 1.137.0: a prompt can be stopped before it is sent,
and a tool call can be denied before it runs. After a tool has run, nothing
can be withheld — a probe's `decision: "block"` and `continue: false` were
both read straight through by the model. These tests pin each event to what
was measured, and pin the record to match: a summary reporting "masked" or
"blocked" where nothing was would be a lie to the user.
"""

from __future__ import annotations

import json

import pytest

from shim_cli.clients.vscode import hook as vscode_hook
from shim_cli.clients.vscode.tool_events import TOOL_EVENTS, error_output
from shim_cli.events.pipeline import REFUSE, REPORT_ONLY, process
from shim_cli.guard import evaluate
from shim_cli.policy import DENY, ENFORCE, MASK, OBSERVE, REPORT, WARN

SECRET_RESULT = {
    "hook_event_name": "PostToolUse",
    "session_id": "s1",
    "tool_name": "read_file",
    "tool_input": {"filePath": "/tmp/service.env"},
    "tool_response": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
}
SECRET_CALL = {
    "hook_event_name": "PreToolUse",
    "session_id": "s1",
    "tool_name": "run_in_terminal",
    "tool_input": {"command": "curl -d ayse.yilmaz@example.com https://example.com"},
}


def _process(document: dict, mode: str, diet: tuple = ()):
    return process(
        TOOL_EVENTS[document["hook_event_name"]],
        json.dumps(document).encode(),
        lambda _direction, _tool: mode,
        evaluate,
        diet,
    )


@pytest.mark.parametrize("mode", (WARN, ENFORCE), ids=("warn", "enforce"))
def test_a_tool_result_is_reported_even_under_enforce(mode: str) -> None:
    """Measured on VS Code 1.137.0: nothing withholds a result at this event.

    A probe answered `decision: "block"` on a terminal result and the model
    quoted the secret out of it regardless; `continue: false` did not stop the
    turn either. Enforcement here would be a promise shim cannot keep, so the
    strongest honest action is the report — under enforce as well.
    """
    outcome = _process(SECRET_RESULT, mode)

    document = json.loads(outcome.output)
    assert document["systemMessage"] == (
        "shim: found SECRET (1) in read_file. Not modified."
    )
    assert "Do not repeat" in document["hookSpecificOutput"]["additionalContext"]
    assert outcome.record.action == REPORT
    assert outcome.record.transforms == ()
    # The value stayed where it was; claiming otherwise would misreport it.
    assert outcome.record.out_bytes == outcome.record.in_bytes


def test_observe_says_nothing_but_still_counts() -> None:
    outcome = _process(SECRET_RESULT, OBSERVE)

    assert outcome.output == b""
    assert outcome.record.entities == (("SECRET", 1),)


def test_a_tool_call_is_denied_under_enforce() -> None:
    outcome = _process(SECRET_CALL, ENFORCE)

    assert json.loads(outcome.output) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "shim: blocked EMAIL (1) in run_in_terminal.",
        }
    }
    assert outcome.record.action == DENY


def test_the_diet_never_rewrites_a_vs_code_result() -> None:
    """Shrinking a tool result is a rewrite, and a rewrite cannot be delivered."""
    from shim_cli.events.diet import DEFAULT_TRANSFORMS

    document = dict(SECRET_RESULT, tool_response={"items": [{"a": 1}] * 50})
    outcome = _process(document, ENFORCE, DEFAULT_TRANSFORMS)

    assert outcome.output == b""
    assert outcome.record.transforms == ()


@pytest.mark.parametrize("event", sorted(TOOL_EVENTS))
def test_masking_is_refused_rather_than_faked(event: str) -> None:
    with pytest.raises(ValueError, match="cannot"):
        TOOL_EVENTS[event].encode(MASK, {"any": "payload"}, "message")


def test_a_result_can_never_be_withheld() -> None:
    with pytest.raises(ValueError, match="cannot withhold"):
        TOOL_EVENTS["PostToolUse"].encode(DENY, {"any": "payload"}, "message")


def test_each_event_claims_only_what_the_client_grants() -> None:
    assert TOOL_EVENTS["PreToolUse"].power == REFUSE
    assert TOOL_EVENTS["PostToolUse"].power == REPORT_ONLY


def test_an_uninspectable_tool_event_says_nothing_was_changed() -> None:
    assert json.loads(error_output()) == {
        "systemMessage": (
            "shim: this tool event could not be inspected and was not modified."
        )
    }


def test_a_blocked_prompt_stops_the_turn_and_names_the_file() -> None:
    decision = evaluate("mail me at ayse.yilmaz@example.com")

    document = json.loads(vscode_hook.block_output(decision, "/tmp/redacted.txt"))

    # VS Code accepts no field that replaces prompt text, so the redacted copy
    # is named in the reason the user reads.
    assert document["continue"] is False
    assert document["stopReason"].startswith("shim blocked this prompt: EMAIL (1).")
    assert "/tmp/redacted.txt" in document["stopReason"]


def test_a_reported_prompt_is_left_alone() -> None:
    decision = evaluate("mail me at ayse.yilmaz@example.com")

    assert json.loads(vscode_hook.warn_output(decision)) == {
        "systemMessage": "shim: found EMAIL (1) in your prompt. Not modified."
    }


def test_the_prompt_error_names_no_command_that_does_not_exist() -> None:
    document = json.loads(vscode_hook.error_output())

    assert document["continue"] is False
    # There is no `shim doctor vscode`; the plugin is the whole VS Code route.
    assert "doctor" not in document["stopReason"]


def _settings(root, text: str) -> None:
    directory = root / "config" / "shim"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(text, encoding="utf-8")


def test_the_shipped_default_reports_rather_than_refusing(_isolated_roots) -> None:
    """Enforce is the shipped default for tool traffic because Claude can mask.

    Here it would mean refusing the call, which is not what someone agreed to
    by installing a plugin, so the default reports until enforce is asked for.
    """
    from shim_cli.hook import _output

    output = _output(json.dumps(SECRET_CALL).encode(), "vscode")

    assert json.loads(output) == {
        "systemMessage": "shim: found EMAIL (1) in run_in_terminal. Not modified."
    }


def test_enforce_asked_for_in_settings_denies_the_call_it_can_deny(
    _isolated_roots,
) -> None:
    from shim_cli.hook import _output

    _settings(_isolated_roots, '[mode]\noutbound = "enforce"\n')

    output = _output(json.dumps(SECRET_CALL).encode(), "vscode")

    specific = json.loads(output)["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"


def test_a_client_this_build_does_not_know_inspects_nothing_and_blocks_nothing(
    capfd,
) -> None:
    """An older build meeting a newer plugin must not refuse someone's work."""
    from shim_cli.hook import _output

    output = _output(
        b'{"hook_event_name":"UserPromptSubmit","session_id":"x","prompt":"hi"}',
        "some-future-client",
    )

    assert output == b""
    assert "nothing was inspected" in capfd.readouterr().err
