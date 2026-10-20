"""The names of the placeholders shim issued in a session, kept beside its log.

Names only: a type and a run of numbers. The conftest gives every test its
own temporary directory.
"""

from __future__ import annotations

import pytest

from shim_cli.session import spool


def _store():
    [path] = list(spool.root_path().glob("*.names"))
    return path


def test_issued_names_are_kept_as_runs_of_numbers_per_type() -> None:
    spool.issue("s", (("EMAIL", 1, 3), ("SECRET", 1, 1)))
    spool.issue("s", (("EMAIL", 5, 5),))

    assert spool.issued("s") == [("EMAIL", 1, 3), ("SECRET", 1, 1), ("EMAIL", 5, 5)]
    assert _store().read_bytes() == b"EMAIL 1 3\nSECRET 1 1\nEMAIL 5 5\n"


def test_a_run_already_covered_is_not_written_again() -> None:
    spool.issue("s", (("EMAIL", 1, 50_000),))
    spool.issue("s", (("EMAIL", 1, 2), ("EMAIL", 7, 7)))

    assert _store().read_bytes() == b"EMAIL 1 50000\n"


def test_a_session_without_a_store_issued_nothing() -> None:
    assert spool.issued("never") == []


def test_sessions_keep_their_own_names() -> None:
    spool.issue("one", (("EMAIL", 1, 1),))

    assert spool.issued("two") == []


def test_clearing_the_session_removes_its_names() -> None:
    spool.issue("s", (("EMAIL", 1, 1),))
    spool.append("s", {"action": "mask"})

    spool.clear("s")

    assert list(spool.root_path().iterdir()) == []


def test_a_store_that_is_not_private_is_refused() -> None:
    spool.issue("s", (("EMAIL", 1, 1),))
    _store().chmod(0o644)

    with pytest.raises(spool.SpoolError):
        spool.issued("s")
    with pytest.raises(spool.SpoolError):
        spool.issue("s", (("EMAIL", 2, 2),))


def test_a_full_store_refuses_rather_than_forgets() -> None:
    _ = spool.issued("s")  # creates the folder
    path = spool.root_path() / f"{spool.session_key('s')}.names"
    path.write_bytes(b"EMAIL 1 1\n" * (spool.MAX_NAMES_BYTES // 10))
    path.chmod(0o600)

    with pytest.raises(spool.SpoolError):
        spool.issue("s", (("SECRET", 1, 1),))


@pytest.mark.parametrize("line", (b"garbage\n", b"EMAIL x 2\n", b"EMAIL 1\n"))
def test_a_store_that_does_not_parse_is_unreadable(line: bytes) -> None:
    spool.issue("s", (("EMAIL", 1, 1),))
    with _store().open("ab") as stream:
        stream.write(line)

    with pytest.raises(spool.SpoolError):
        spool.issued("s")
