"""`shim audit` counts what shim masked from shim's own records (F081).

Placeholder-shaped text in a transcript is not evidence: shim's docs, a test
fixture or a file the model wrote can hold `<EMAIL_1>`. The conftest isolates
the temporary directory, the state folder and the settings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shim_cli.cli.app import app
from shim_cli.config import config_path
from shim_cli.session import ledger, spool

runner = CliRunner()
SESSION = "cccc3333-0000-4000-8000-000000000003"
DAY = "2026-09-28T12:00:00.000Z"


@pytest.fixture
def transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    path = config / "projects" / "-work-docs" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        {
            "type": "assistant",
            "cwd": "/work/docs",
            "timestamp": DAY,
            "message": {
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read"}]
            },
        },
        {
            "type": "user",
            "cwd": "/work/docs",
            "timestamp": DAY,
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "Docs example: shim answers with <EMAIL_1> "
                        "and <SECRET_1>.",
                    }
                ]
            },
        },
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _masked(*arguments: str) -> dict:
    result = runner.invoke(app, ["audit", "--json", *arguments])
    payload = json.loads(result.stdout)
    assert payload["by_session"][SESSION]["masked"] == payload["masked"]["values"]
    return payload["masked"]


def _record(action: str, entities: dict, ts: str = "2026-09-28T12:00:01Z") -> dict:
    return {
        "session_id": spool.session_key(SESSION),
        "action": action,
        "entities": entities,
        "ts": ts,
    }


def test_placeholders_in_a_transcript_are_not_counted_as_masked(transcript) -> None:
    assert _masked() == {"values": 0, "sessions": 0, "ledger": False}
    assert not spool.root_path().exists()  # reading the records wrote nothing
    assert (
        "already masked by shim       none (without the ledger, live sessions only)"
        in (runner.invoke(app, ["audit"]).stdout)
    )


def test_a_live_session_is_counted_from_its_spool(transcript) -> None:
    spool.append(SESSION, _record("mask", {"EMAIL": 2, "SECRET": 1}))
    spool.append(SESSION, _record("report", {"EMAIL": 5}))
    spool.append("another-session", _record("mask", {"EMAIL": 7}))

    assert _masked() == {"values": 3, "sessions": 1, "ledger": False}


def test_an_ended_session_is_counted_from_the_ledger_once(transcript) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("ledger = true\n")
    path.chmod(0o600)
    entry = _record("mask", {"SECRET": 1})
    ledger.append(entry)
    spool.append(SESSION, entry)  # the same event, still in a live session

    ledger.append(_record("mask", {"EMAIL": 4}, "2026-09-27T00:00:00Z"))

    assert _masked() == {"values": 5, "sessions": 1, "ledger": True}
    assert _masked("--since", "2026-09-28") == {
        "values": 1,
        "sessions": 1,
        "ledger": True,
    }


def test_a_live_session_counts_only_masks_since_the_cut(transcript) -> None:
    spool.append(SESSION, _record("mask", {"EMAIL": 4}, "2026-09-27T00:00:00Z"))
    spool.append(SESSION, _record("mask", {"SECRET": 1}))

    assert _masked("--since", "2026-09-28") == {
        "values": 1,
        "sessions": 1,
        "ledger": False,
    }
