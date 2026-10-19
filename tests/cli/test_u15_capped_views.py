"""`shim report` and `shim ledger show` say when what they show ends early."""

from __future__ import annotations

import datetime
import json

from click import unstyle
from typer.testing import CliRunner

from shim_cli.cli.app import app
from shim_cli.session import ledger, spool

runner = CliRunner()
JANUARY = datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc)
FEBRUARY = datetime.datetime(2026, 2, 15, tzinfo=datetime.timezone.utc)
SHORT = "some events were not kept"
ENTRY = {
    "session_id": "ended",
    "ts": "2026-01-15T09:00:00Z",
    "action": "mask",
    "entities": {"SECRET": 1},
}


def _text(*arguments: str) -> str:
    return " ".join(unstyle(runner.invoke(app, list(arguments)).output).split())


def _lose_in_january_then_write_in_february() -> None:
    ledger.append(ENTRY, JANUARY)
    ledger.root_path().joinpath("ledger-2026-01.lost").touch(mode=0o600)
    ledger.append({**ENTRY, "ts": "2026-02-15T09:00:00Z"}, FEBRUARY)


def test_show_still_warns_when_an_earlier_month_lost_an_event() -> None:
    _lose_in_january_then_write_in_february()

    assert SHORT in _text("ledger", "show")
    assert json.loads(runner.invoke(app, ["ledger", "show", "--json"]).output)["capped"]


def test_show_warns_when_the_only_event_of_the_ledger_was_lost() -> None:
    ledger.root_path().mkdir(mode=0o700, parents=True, exist_ok=True)
    ledger.root_path().joinpath("ledger-2026-01.jsonl").touch(mode=0o600)
    ledger.root_path().joinpath("ledger-2026-01.lost").touch(mode=0o600)

    text = _text("ledger", "show")

    assert SHORT in text
    assert "recorded nothing yet" not in text
    assert "The ledger is empty" not in text
    assert json.loads(runner.invoke(app, ["ledger", "show", "--json"]).output)["capped"]


def test_the_report_fallback_still_warns_when_an_earlier_month_is_at_its_cap() -> None:
    ledger.append(ENTRY, JANUARY)
    path = ledger.files()[0]
    with path.open("ab") as stream:
        stream.write(b"\n" * (ledger.MAX_LEDGER_BYTES - path.stat().st_size - 100))
    ledger.append({**ENTRY, "ts": "2026-02-15T09:00:00Z"}, FEBRUARY)

    assert SHORT in _text("report")
    assert json.loads(runner.invoke(app, ["report", "--json"]).output)["capped"]


def test_report_says_the_live_session_is_incomplete_when_no_record_of_it_survived() -> (
    None
):
    spool.root_path().mkdir(mode=0o700, parents=True, exist_ok=True)
    stem = spool.session_key("live")
    spool.root_path().joinpath(f"{stem}.jsonl").touch(mode=0o600)
    spool.root_path().joinpath(f"{stem}.lost").touch(mode=0o600)

    text = _text("report")
    document = json.loads(runner.invoke(app, ["report", "--json"]).output)

    assert "session log is incomplete" in text
    assert "No session on record" not in text
    assert (document["source"], document["events"], document["capped"]) == (
        "session",
        0,
        True,
    )
