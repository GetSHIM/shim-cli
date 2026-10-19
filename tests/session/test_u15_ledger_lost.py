"""A ledger event lost to a lock held past the wait is noted, like the spool's."""

from __future__ import annotations

import datetime
import fcntl
import os
from pathlib import Path

import pytest

from shim_cli.session import ledger

JANUARY = datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc)
FEBRUARY = datetime.datetime(2026, 2, 15, tzinfo=datetime.timezone.utc)
ENTRY = {"action": "mask", "entities": {"SECRET": 1}, "ts": "2026-01-15T00:00:00Z"}


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def _lose_one() -> None:
    ledger.append(ENTRY, JANUARY)
    descriptor = os.open(ledger.files()[0], os.O_RDWR | os.O_NOFOLLOW)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        with pytest.raises(ledger.LedgerError):
            ledger.append(ENTRY, JANUARY)
    finally:
        os.close(descriptor)


def test_an_event_lost_to_a_stuck_lock_makes_the_month_say_it_is_short() -> None:
    assert ledger.capped() is False

    _lose_one()

    assert ledger.capped() is True
    assert len(ledger.entries()) == 1
    assert [path.name for path in ledger.files()] == ["ledger-2026-01.jsonl"]


def test_a_loss_in_an_earlier_month_is_still_said_in_the_next() -> None:
    _lose_one()
    ledger.append(ENTRY, FEBRUARY)

    assert ledger.capped() is True
    assert len(ledger.files()) == 2


def test_purge_and_prune_remove_the_note_with_its_month() -> None:
    _lose_one()
    assert ledger.prune(datetime.datetime(2027, 1, 1, tzinfo=datetime.timezone.utc))
    assert sorted(path.name for path in ledger.root_path().iterdir()) == []

    _lose_one()
    assert ledger.purge() == 1
    assert list(ledger.root_path().iterdir()) == []
    assert ledger.capped() is False
