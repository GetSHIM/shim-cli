from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def _run(raw: bytes, client: str = "claude", command: tuple = ()) -> bytes:
    result = subprocess.run(
        command or (sys.executable, "-I", "-B", "-m", "shim_cli.hook", client),
        input=raw,
        capture_output=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


MALFORMED_TOOL_EVENTS = (
    b'{"hook_event_name":"PreToolUse","tool_name":123,"tool_input":{"a":"b"}}',
    b'{"hook_event_name":"PostToolUse","tool_name":123,"tool_response":{"a":"b"}}',
    b'{"hook_event_name":"PreToolUse","tool_name":{"a":1},"tool_input":{}}',
)


def _denied(tool: str, reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"shim: could not inspect this {tool} call ({reason}), so it was "
                "not allowed."
            ),
        }
    }


@pytest.mark.parametrize("raw", MALFORMED_TOOL_EVENTS[0::2])
def test_a_malformed_tool_call_is_denied_not_passed(raw: bytes) -> None:
    """Never the prompt's block, and never a call shim did not look at."""
    assert json.loads(_run(raw)) == _denied("unknown tool", "analysis failed")


def test_a_malformed_tool_result_is_withheld_not_passed() -> None:
    document = json.loads(_run(MALFORMED_TOOL_EVENTS[1]))

    assert "decision" not in document, "the prompt's block has no place here"
    output = document["hookSpecificOutput"]
    # A tool with no name is no built-in tool whose shape Claude Code checks,
    # so not even a key of the result is kept.
    assert output["updatedToolOutput"] == [
        {"type": "text", "text": "[withheld by shim]"}
    ]
    assert "(analysis failed)" in document["systemMessage"]


@pytest.mark.parametrize("raw", MALFORMED_TOOL_EVENTS)
def test_copilot_says_nothing_rather_than_guessing_a_shape(raw: bytes) -> None:
    assert _run(raw, "copilot") == b""


def test_an_unsupported_tool_event_does_not_guess_a_native_shape() -> None:
    raw = b'{"hook_event_name":"NotARealEvent","tool_name":"Read","tool_input":{}}'

    for client in ("claude", "codex", "copilot"):
        assert _run(raw, client) == b""


def test_a_malformed_prompt_event_still_fails_closed() -> None:
    document = json.loads(_run(b'{"hook_event_name":"UserPromptSubmit","prompt":7}'))

    assert document["decision"] == "block"


def test_an_over_cap_tool_call_is_denied() -> None:
    head = b'{"hook_event_name":"PreToolUse","session_id":"s","tool_name":"Write",'
    raw = head + b'"tool_input":{"content":"' + b"A" * 1_100_000

    assert json.loads(_run(raw)) == _denied("Write", "too large")


def test_an_over_cap_built_in_result_tells_the_model_it_was_not_inspected() -> None:
    """Its shape is cut off at the read limit, and Claude Code sends the
    original whenever a built-in tool's replacement does not match that tool's
    schema; the honest answer left is to tell both the user and the model."""
    head = b'{"hook_event_name":"PostToolUse","session_id":"s","tool_name":"Read",'
    raw = head + b'"tool_response":{"content":"' + b"A" * 1_100_000

    document = json.loads(_run(raw))

    assert "decision" not in document
    assert document["systemMessage"] == (
        "shim: could not inspect this Read output (too large); the model has it "
        "as it was."
    )
    assert document["hookSpecificOutput"]["additionalContext"] == (
        "shim could not inspect this Read output (too large). Do not repeat "
        "values from it in replies, files or commands."
    )


def test_an_over_cap_prompt_payload_still_fails_closed() -> None:
    raw = b'{"hook_event_name":"UserPromptSubmit","prompt":"' + b"A" * 1_100_000

    document = json.loads(_run(raw))

    assert document["decision"] == "block"


def test_a_file_name_cannot_carry_terminal_escapes_into_the_summary() -> None:
    from shim_cli.session import spool, summary

    hostile = "/work/" + chr(27) + "[31mred" + chr(7) + "/config.env"
    _run(
        json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "escapes",
                "tool_name": "Read",
                "tool_input": {"file_path": hostile},
                "tool_response": {
                    "type": "text",
                    "file": {"content": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"},
                },
            }
        ).encode()
    )

    records = spool.entries("escapes")
    assert records, "the event was not recorded"
    rendered = summary.render(records) + json.dumps(records)
    assert chr(27) not in rendered
    assert chr(7) not in rendered
    assert "config.env" in summary.render(records)


@pytest.mark.parametrize(
    "tool",
    (f"Read{chr(27)}[31m", "T" * 121),
    ids=("control-character", "over-bound"),
)
def test_tool_labels_are_safe_in_the_spool_and_summary(tool: str) -> None:
    from shim_cli.session import spool, summary

    _run(
        json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "unsafe-tool-label",
                "tool_name": tool,
                "tool_response": {"text": "contact synthetic.user@example.com"},
            }
        ).encode()
    )

    records = spool.entries("unsafe-tool-label")
    assert len(records) == 1
    assert records[0]["entities"] == {"EMAIL": 1}
    assert records[0]["tool_name"] == "unknown tool"
    rendered = summary.render(records)
    structured = json.dumps(summary.as_json(records))
    stored = json.dumps(records)
    assert "unknown tool" in rendered + structured + stored
    assert tool not in rendered
    assert json.dumps(tool)[1:-1] not in structured + stored


def test_an_uninspectable_tool_event_is_still_recorded(monkeypatch, tmp_path) -> None:
    from shim_cli import hook
    from shim_cli.session import spool
    from shim_cli.session.record import NOT_INSPECTED

    def explode(*_args, **_kwargs):
        raise ValueError("Guard analysis exceeded the safe finding limit.")

    monkeypatch.setattr(hook, "_tool_output", explode)
    payload = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "dense",
            "tool_name": "Read",
            "tool_input": {"file_path": "/work/customers.csv"},
            "tool_response": {"type": "text", "content": "a@b.com"},
        }
    ).encode()

    output = hook._output(payload, "claude")

    document = json.loads(output)
    assert document["hookSpecificOutput"]["updatedToolOutput"] == {
        "type": "text",
        "content": "",
    }
    assert b"a@b.com" not in output
    records = spool.entries("dense")
    assert len(records) == 1
    assert records[0]["action"] == "deny"
    assert records[0]["event"] == "PostToolUse"
    assert records[0]["tool_name"] == "Read"
    assert records[0]["note"] == f"{NOT_INSPECTED}: analysis failed"


def test_uninspected_records_never_keep_raw_event_or_tool_labels(
    monkeypatch, tmp_path
) -> None:
    from shim_cli import hook
    from shim_cli.session import spool, summary

    event = f"Unexpected{chr(27)}Event" + "X" * 256
    tool = "alice@example.com"
    payload = json.dumps(
        {
            "hook_event_name": event,
            "session_id": "unsafe-labels",
            "tool_name": tool,
            "tool_response": {"text": "synthetic.user@example.com"},
        }
    ).encode()

    # No adapter answers an event it does not know, so only the record is kept.
    assert hook._not_inspected(payload, "claude", event, "unsafe-labels") == b""

    records = spool.entries("unsafe-labels")
    assert len(records) == 1
    assert records[0]["event"] == "unsupported tool event"
    assert records[0]["tool_name"] == "<EMAIL_1>"
    output = summary.render(records) + json.dumps(summary.as_json(records))
    stored = json.dumps(records)
    assert event not in output + stored
    assert tool not in output + stored


def test_detector_failure_still_records_an_unknown_tool(monkeypatch) -> None:
    from shim_cli import guard, hook
    from shim_cli.session import spool

    def explode(*_args, **_kwargs):
        raise RuntimeError("detector unavailable")

    monkeypatch.setattr(guard, "evaluate", explode)
    payload = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "failed-detector",
            "tool_name": "alice@example.com",
            "tool_response": {"text": "safe"},
        }
    ).encode()

    hook._output(payload, "claude")

    records = spool.entries("failed-detector")
    assert len(records) == 1
    assert records[0]["tool_name"] == "unknown tool"
    assert "alice@example.com" not in json.dumps(records)


def test_the_summary_says_which_uninspected_events_the_model_still_read(
    unanalysable: str, unanalysable_hook: tuple
) -> None:
    """Withheld or refused is not passed through: the summary says which,
    from what the hook told the client."""
    from shim_cli.session import spool, summary

    big = "x" * 1_100_000
    # Read in part: the key was found before the field beside it failed.
    part_read = [
        {"type": "text", "text": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"},
        {"type": "text", "text": unanalysable},
    ]
    events = (
        ("PostToolUse", "mcp__drive__read", "tool_response", [{"text": big}]),
        ("PreToolUse", "Write", "tool_input", {"file_path": "/w/a", "content": big}),
        ("PostToolUseFailure", "Bash", "error", 5),
        ("PostToolUse", "mcp__sheets__read", "tool_response", part_read),
    )
    for event, tool, root, value in events:
        _run(
            json.dumps(
                {
                    "session_id": "mixed",
                    "permission_mode": "default",
                    "hook_event_name": event,
                    "tool_name": tool,
                    root: value,
                }
            ).encode(),
            command=unanalysable_hook,
        )

    records = spool.entries("mixed")
    text = summary.render(records)

    assert [record["action"] for record in records] == [
        "deny",
        "deny",
        "report",
        "deny",
    ]
    assert records[-1]["note"] == "not inspected: analysis failed"
    # The key found before the read stopped never reached the model either.
    assert text.splitlines()[1:4] == [
        "  blocked   1 SECRET  (mcp__sheets__read)",
        "  skipped   3 not inspected, withheld or refused  "
        "(Write, mcp__drive__read, mcp__sheets__read)",
        "            1 not inspected, passed through  (failed Bash)",
    ]
    assert "incomplete inspection" not in text
    assert summary.as_json(records)["not_inspected"] == 4


def test_the_finding_for_uninspected_events_does_not_say_they_passed() -> None:
    import datetime

    from shim_cli.cli.findings import from_records
    from shim_cli.session import spool

    _run(
        json.dumps(
            {
                "session_id": "finding",
                "permission_mode": "default",
                "hook_event_name": "PostToolUse",
                "tool_name": "mcp__drive__read",
                "tool_response": [{"text": "x" * 1_100_000}],
            }
        ).encode()
    )

    [finding] = from_records(
        spool.entries("finding"), datetime.datetime.now(datetime.timezone.utc)
    )

    assert finding["rule_id"] == "cli.session.not_inspected"
    assert finding["title"] == "Tool events shim could not inspect"
    assert finding["summary"]["en"] == (
        "In this Claude Code session shim could not inspect 1 event(s) and "
        "inspected 0 only in part."
    )
    assert finding["remediation"]["text"]["en"] == (
        "Run shim report to see which tool or file it was, and shim doctor "
        "claude to check the install."
    )


def test_a_refused_event_names_its_session_as_a_read_one_does() -> None:
    """Copilot spells it `sessionId`; the reader for a document too large or
    broken to parse takes either spelling, as the full parser does."""
    from shim_cli import hook

    head = b'{"hook_event_name":"PostToolUse","sessionId":"camel","tool_name":"x",'
    for raw in (head + b'"tool_response":"y"}', head + b'"tool_response":"' + b"y" * 9):
        assert hook._refused_envelope(raw)[:2] == ("PostToolUse", "camel")


def test_a_broken_detector_still_withholds_a_built_in_result_in_its_shape(
    monkeypatch,
) -> None:
    """With the detector down even the tool's label cannot be checked, so the
    user reads "unknown tool"; the result is still a Read, and keeps the shape
    Claude Code checks a Read against, or it would be sent as it was."""
    from shim_cli import guard, hook
    from shim_cli.session import spool

    def explode(*_args, **_kwargs):
        raise RuntimeError("detector unavailable")

    monkeypatch.setattr(guard, "evaluate", explode)
    payload = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "broken-detector",
            "tool_name": "Read",
            "tool_input": {"file_path": "/work/.env"},
            "tool_response": {
                "type": "text",
                "file": {
                    "filePath": "/work/.env",
                    "content": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n",
                    "numLines": 1,
                },
            },
        }
    ).encode()

    output = hook._output(payload, "claude")

    assert b"AKIA" not in output
    assert json.loads(output)["hookSpecificOutput"]["updatedToolOutput"] == {
        "type": "text",
        "file": {"filePath": "", "content": "[withheld by shim]", "numLines": 0},
    }
    [record] = spool.entries("broken-detector")
    assert (record["tool_name"], record["action"]) == ("unknown tool", "deny")


REFUSAL_CASES = (
    ("a parseable tool event", b'{"hook_event_name":"PreToolUse"}', True),
    ("a parseable prompt event", b'{"hook_event_name":"UserPromptSubmit"}', False),
    ("an unnamed event", b'{"prompt":"hello"}', False),
    ("a truncated tool payload", b'{"hook_event_name":"PostToolUse","tool_re', True),
    ("a truncated prompt payload", b'{"hook_event_name":"UserPromptSubm', False),
    ("nothing at all", b"", False),
)


@pytest.mark.parametrize(
    ("payload", "is_tool"),
    [case[1:] for case in REFUSAL_CASES],
    ids=[case[0] for case in REFUSAL_CASES],
)
def test_the_refusal_shape_follows_the_event(payload: bytes, is_tool: bool) -> None:
    from shim_cli import hook

    for client in ("claude", "codex"):
        output = hook._refusal_output(payload, client)

        assert (b'"decision":"block"' in output) is not is_tool, (
            f"{client} got the wrong shape for {payload!r}"
        )


def test_the_deadline_on_a_tool_call_denies_it() -> None:
    from shim_cli.session import spool
    from shim_cli.session.record import NOT_INSPECTED

    code = (
        "import sys, time\n"
        "from shim_cli import hook as runner\n"
        "sys.argv.append('claude')\n"
        "runner.HOOK_DEADLINE_SECONDS = 0.2\n"
        "runner._output = lambda *_arguments: time.sleep(5)\n"
        "runner.main()\n"
    )
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "slow",
            "tool_name": "Read",
            "tool_input": {"file_path": "/work/notes.md"},
        }
    ).encode()

    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", code),
        input=payload,
        capture_output=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout) == _denied("Read", "deadline")
    records = spool.entries("slow")
    assert [(record["action"], record["note"]) for record in records] == [
        ("deny", f"{NOT_INSPECTED}: deadline")
    ]


def test_session_end_sweeps_stale_redacted_prompts(tmp_path, monkeypatch) -> None:
    import time

    from shim_cli import hook

    monkeypatch.setattr(hook.tempfile, "gettempdir", lambda: str(tmp_path))
    stale = tmp_path / "shim-redacted-old.txt"
    stale_020 = tmp_path / "shim-guard-redacted-old.txt"
    fresh = tmp_path / "shim-redacted-new.txt"
    other = tmp_path / "someone-elses-file.txt"
    for path in (stale, stale_020, fresh, other):
        path.write_text("redacted", encoding="utf-8")
    old_enough = time.time() - hook.SUGGESTION_MAX_AGE_SECONDS - 60
    os.utime(stale, (old_enough, old_enough))
    os.utime(stale_020, (old_enough, old_enough))
    os.utime(other, (old_enough, old_enough))

    hook._forget("any-session")

    assert not stale.exists()
    assert not stale_020.exists()
    assert fresh.exists()
    assert other.exists()
