from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", str(tmp_path / "spools"))


def _run(raw: bytes, client: str = "claude") -> bytes:
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-m", "shim_cli.hook", client),
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


@pytest.mark.parametrize("raw", MALFORMED_TOOL_EVENTS)
def test_a_malformed_tool_event_is_never_answered_with_a_block(raw: bytes) -> None:
    document = json.loads(_run(raw))

    assert "decision" not in document, "a block here denies the tool call"
    assert "permissionDecision" not in document
    assert "hookSpecificOutput" not in document
    assert document["systemMessage"].startswith("shim:")
    assert "prompt" not in document["systemMessage"]


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


def test_an_over_cap_tool_payload_does_not_deny_the_tool_call() -> None:
    head = b'{"hook_event_name":"PostToolUse","session_id":"s","tool_name":"Read",'
    raw = head + b'"tool_response":{"content":"' + b"A" * 1_100_000

    document = json.loads(_run(raw))

    assert "decision" not in document
    assert document["systemMessage"].startswith("shim:")


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
    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", str(tmp_path / "spools"))
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

    assert b"could not be inspected" in output
    records = spool.entries("dense")
    assert len(records) == 1
    assert records[0]["action"] == "report"
    assert records[0]["event"] == "PostToolUse"
    assert records[0]["tool_name"] == "Read"
    assert records[0]["note"].startswith(NOT_INSPECTED)


def test_uninspected_records_never_keep_raw_event_or_tool_labels(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", str(tmp_path / "spools"))
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

    hook._uninspected(payload, "claude", event, "unsafe-labels")

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


def test_the_summary_names_an_uninspected_event(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", str(tmp_path / "spools"))
    from shim_cli.session import spool, summary
    from shim_cli.session.record import NOT_INSPECTED

    spool.append(
        "dense",
        {
            "action": "report",
            "event": "PostToolUse",
            "tool_name": "Read",
            "target": "/work/customers.csv",
            "entities": {},
            "note": f"{NOT_INSPECTED}: analysis failed",
            "latency_ms": 9,
        },
    )

    text = summary.render(spool.entries("dense"))

    assert "skipped" in text
    assert "not inspected, passed through" in text
    assert "Read customers.csv" in text
    assert summary.as_json(spool.entries("dense"))["not_inspected"] == 1


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


def test_the_deadline_on_a_tool_event_does_not_deny_the_call() -> None:
    code = (
        "import sys, time\n"
        "from shim_cli import hook as runner\n"
        "sys.argv.append('claude')\n"
        "runner.HOOK_DEADLINE_SECONDS = 0.2\n"
        "runner._output = lambda raw, client='codex': time.sleep(5)\n"
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
    assert b'"decision":"block"' not in result.stdout
    assert b"could not be inspected" in result.stdout


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
