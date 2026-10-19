"""A hook that cannot take the session lock still masks, and the Stop summary
says the log is short instead of presenting a partial count as complete."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

from shim_cli.session import spool

ROOT = Path(__file__).parents[2]
SESSION = "0199aa11-2233-4455-6677-889900aabbcc"


def _run(payload: dict) -> bytes:
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-m", "shim_cli.hook", "claude"),
        input=json.dumps(payload).encode(),
        capture_output=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


def _stop() -> dict:
    return {"hook_event_name": "Stop", "session_id": SESSION}


def _read_event(path: str) -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": SESSION,
        "tool_name": "Read",
        "tool_input": {"file_path": path},
        "tool_response": {
            "type": "text",
            "file": {"content": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"},
        },
    }


def test_a_record_lost_to_a_stuck_lock_is_declared_by_the_summary() -> None:
    _run(_read_event("/work/service/.env"))
    log = next(spool.root_path().glob("*.jsonl"))
    descriptor = os.open(log, os.O_RDWR | os.O_NOFOLLOW)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        output = json.loads(_run(_read_event("/work/other/.env")))
    finally:
        os.close(descriptor)

    assert "<SECRET_1>" in json.dumps(output), "the mask itself must not depend on it"
    assert len(spool.entries(SESSION)) == 1
    message = json.loads(_run(_stop()))["systemMessage"]
    assert "1 SECRET" in message
    assert message.endswith(
        "(session log is incomplete: it reached its size cap or a record "
        "could not be written; some events are not counted)"
    )


def test_a_stop_during_a_stuck_lock_is_made_up_by_the_next_one() -> None:
    _run(_read_event("/work/service/.env"))
    log = next(spool.root_path().glob("*.jsonl"))
    descriptor = os.open(log, os.O_RDWR | os.O_NOFOLLOW)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        # The stuck lock outlasts the wait: the Stop is withheld, not lost.
        assert _run(_stop()) == b""
    finally:
        os.close(descriptor)

    assert "1 SECRET" in json.loads(_run(_stop()))["systemMessage"]


def _stuck(path: Path) -> int:
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


INCOMPLETE = "session log is incomplete"


def test_a_record_lost_after_a_stop_is_declared_by_the_next_one() -> None:
    _run(_read_event("/work/service/.env"))
    assert "1 SECRET" in json.loads(_run(_stop()))["systemMessage"]
    descriptor = _stuck(next(spool.root_path().glob("*.jsonl")))
    try:
        _run(_read_event("/work/other/.env"))
    finally:
        os.close(descriptor)

    # No record landed since the Stop, yet one was lost: that must be said.
    message = json.loads(_run(_stop()))["systemMessage"]
    assert "1 SECRET" in message
    assert INCOMPLETE in message
    # Said once: the same loss is not repeated at every later Stop.
    assert _run(_stop()) == b""


def test_a_session_whose_first_record_was_lost_does_not_read_as_clean() -> None:
    log = spool.root_path() / f"{spool.session_key(SESSION)}.jsonl"
    spool.root_path().mkdir(mode=0o700, exist_ok=True)
    log.touch(mode=0o600)
    descriptor = _stuck(log)
    try:
        _run(_read_event("/work/service/.env"))
    finally:
        os.close(descriptor)

    assert spool.entries(SESSION) == []
    assert INCOMPLETE in json.loads(_run(_stop()))["systemMessage"]


def test_a_record_dropped_at_the_cap_after_a_stop_is_declared_by_the_next_one() -> None:
    _run(_read_event("/work/service/.env"))
    assert "1 SECRET" in json.loads(_run(_stop()))["systemMessage"]
    log = next(spool.root_path().glob("*.jsonl"))
    room = spool.MAX_SPOOL_BYTES - log.stat().st_size - 100
    with log.open("ab") as stream:
        stream.write(b"\n" * room)
    _run(_read_event("/work/other/.env"))
    assert len(spool.entries(SESSION)) == 1, "the second record did not fit"

    message = json.loads(_run(_stop()))["systemMessage"]
    assert INCOMPLETE in message
