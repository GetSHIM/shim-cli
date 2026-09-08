from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from shim_cli.session import SESSION_EVENTS, spool

ROOT = Path(__file__).parents[2]
SESSION = "0199aa11-2233-4455-6677-889900aabbcc"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", str(tmp_path / "spools"))


def _run(payload: dict, client: str = "claude") -> bytes:
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-m", "shim_cli.hook", client),
        input=json.dumps(payload).encode(),
        capture_output=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


def _stop(session_id: str = SESSION, **changes: object) -> dict:
    payload = {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "stop_hook_active": False,
    }
    payload.update(changes)
    return payload


def _read_event(path: str, session_id: str = SESSION) -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": session_id,
        "tool_name": "Read",
        "tool_input": {"file_path": path},
        "tool_response": {
            "type": "text",
            "file": {"content": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"},
        },
    }


def test_a_tool_event_is_recorded_and_the_next_stop_reports_it() -> None:
    _run(_read_event("/work/service/.env"))

    document = json.loads(_run(_stop()))

    assert set(document) == {"systemMessage"}
    assert document["systemMessage"].splitlines()[0] == "shim — this session"
    assert "1 SECRET" in document["systemMessage"]
    assert "Read .env" in document["systemMessage"]
    assert "AKIAIOSFODNN7EXAMPLE" not in document["systemMessage"]


def test_a_second_stop_with_nothing_new_says_nothing() -> None:
    _run(_read_event("/work/service/.env"))
    assert _run(_stop()) != b""

    assert _run(_stop()) == b""


def test_a_stop_after_more_activity_reports_the_session_total() -> None:
    _run(_read_event("/work/service/.env"))
    _run(_stop())

    _run(_read_event("/work/other/.env"))
    document = json.loads(_run(_stop()))

    assert "2 SECRET" in document["systemMessage"]


def test_a_session_where_shim_did_nothing_produces_no_summary() -> None:
    _run(
        {
            "hook_event_name": "PostToolUse",
            "session_id": SESSION,
            "tool_name": "Read",
            "tool_input": {"file_path": "/work/readme.md"},
            "tool_response": {"type": "text", "file": {"content": "nothing here"}},
        }
    )

    assert _run(_stop()) == b""


def test_a_stop_hook_that_is_already_active_does_not_re_emit() -> None:
    _run(_read_event("/work/service/.env"))

    assert _run(_stop(stop_hook_active=True)) == b""


def test_a_stop_without_a_session_is_silent() -> None:
    _run(_read_event("/work/service/.env"))

    assert _run({"hook_event_name": "Stop"}) == b""


def test_session_end_deletes_the_record() -> None:
    _run(_read_event("/work/service/.env"))
    assert spool.entries(SESSION)

    assert _run({"hook_event_name": "SessionEnd", "session_id": SESSION}) == b""

    assert spool.entries(SESSION) == []
    assert list(spool.root_path().iterdir()) == []


def test_recording_failure_never_blocks_a_tool_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unusable = tmp_path / "unusable"
    unusable.mkdir(mode=0o755)
    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", str(unusable))

    output = _run(_read_event("/work/service/.env"))

    document = json.loads(output)
    assert "AKIAIOSFODNN7EXAMPLE" not in output.decode()
    assert document["hookSpecificOutput"]["updatedToolOutput"]
    assert not list(unusable.iterdir())


def test_the_prompt_path_is_recorded_too() -> None:
    _run(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": SESSION,
            "prompt": "Contact alice@example.com",
        }
    )

    records = spool.entries(SESSION)
    assert [record["entities"] for record in records] == [{"EMAIL": 1}]
    assert [record["direction"] for record in records] == ["user-prompt"]
    assert "alice@example.com" not in json.dumps(records)


def test_the_installed_session_events_are_the_ones_the_hook_dispatches() -> None:
    from shim_cli import hook

    assert set(SESSION_EVENTS) == {hook._STOP_EVENT, hook._SESSION_END_EVENT}


REPLY = "I moved the account TR330006100519786457841326 and told alice@example.com."


def test_the_model_s_own_reply_is_counted_at_stop() -> None:
    _run(_read_event("/work/service/.env"))

    document = json.loads(_run(_stop(last_assistant_message=REPLY)))

    message = document["systemMessage"]
    assert "model     1 EMAIL, 1 IBAN in its replies" in message
    assert "(model-generated content, not leaks)" in message
    assert "TR330006100519786457841326" not in message
    assert "alice@example.com" not in message


def test_a_clean_reply_is_not_worth_a_summary() -> None:
    assert _run(_stop(last_assistant_message="All three tests pass.")) == b""


def test_a_reply_is_counted_even_when_the_summary_is_suppressed() -> None:
    assert _run(_stop(stop_hook_active=True, last_assistant_message=REPLY)) == b""

    document = json.loads(_run(_stop()))

    assert "model     1 EMAIL, 1 IBAN in its replies" in document["systemMessage"]


@pytest.mark.parametrize("value", (None, 7, "", ["a"], {"text": "a"}))
def test_a_missing_or_malformed_reply_changes_nothing(value: object) -> None:
    _run(_read_event("/work/service/.env"))
    payload = _stop()
    if value is not None:
        payload["last_assistant_message"] = value

    document = json.loads(_run(payload))

    assert "1 SECRET" in document["systemMessage"]
    assert "in its replies" not in document["systemMessage"]


def test_the_reply_count_reaches_the_report_as_its_own_direction() -> None:
    _run(_stop(last_assistant_message=REPLY))

    records = spool.entries(SESSION)

    assert [record["direction"] for record in records] == ["model-output"]
    assert records[0]["action"] == "report"
    assert records[0]["mode"] == "observe"
    assert records[0]["entities"] == {"EMAIL": 1, "IBAN": 1}
    assert REPLY not in json.dumps(records)


def test_a_reply_past_the_scan_bound_is_counted_short_and_says_so() -> None:
    from shim_cli.guard.normalize import MAX_SOURCE_CHARACTERS

    filler = "fine. " * ((MAX_SOURCE_CHARACTERS // 6) + 100)
    _run(_stop(last_assistant_message=filler + " alice@example.com"))

    records = spool.entries(SESSION)

    assert records[0]["note"] == "truncated"
    assert records[0]["entities"] == {}
    assert records[0]["in_bytes"] > MAX_SOURCE_CHARACTERS


def test_a_reply_just_under_the_bound_is_counted_in_full() -> None:
    from shim_cli.guard.normalize import MAX_SOURCE_CHARACTERS

    filler = "fine. " * ((MAX_SOURCE_CHARACTERS // 6) - 100)
    _run(_stop(last_assistant_message=filler + " alice@example.com"))

    records = spool.entries(SESSION)

    assert records[0]["note"] == ""
    assert records[0]["entities"] == {"EMAIL": 1}
