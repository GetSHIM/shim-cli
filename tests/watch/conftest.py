from __future__ import annotations

import datetime

import pytest

from shim_cli.watch import report


@pytest.fixture(autouse=True)
def _prices_read_today(monkeypatch):
    """The report compares PRICED_ON with today; the suite must not start
    failing 90 days after a price update, so today is the reading date."""
    monkeypatch.setattr(
        report, "_today", lambda: datetime.date.fromisoformat(report.PRICED_ON)
    )
