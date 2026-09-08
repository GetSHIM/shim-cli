from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from shim_cli.session import spool

SESSION = "0199aa11-2233-4455-6677-889900aabbcc"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", str(tmp_path / "spools"))


def _entry(action: str = "mask", **changes: object) -> dict:
    entry = {
        "client": "claude",
        "event": "PostToolUse",
        "tool_name": "Read",
        "target": "/work/.env",
        "action": action,
        "entities": {"SECRET": 1},
        "latency_ms": 7,
    }
    entry.update(changes)
    return entry


def test_records_round_trip_in_order() -> None:
    spool.append(SESSION, _entry())
    spool.append(SESSION, _entry("report", tool_name="Bash"))

    found = spool.entries(SESSION)
    assert [record["tool_name"] for record in found] == ["Read", "Bash"]
    assert found[0]["entities"] == {"SECRET": 1}


def test_the_session_identifier_never_becomes_a_file_name() -> None:
    spool.append(SESSION, _entry())

    names = [path.name for path in spool.root_path().iterdir()]
    assert names == [f"{spool.session_key(SESSION)}.jsonl"]
    assert len(spool.session_key(SESSION)) == 32
    assert SESSION not in names[0]


@pytest.mark.parametrize(
    "hostile",
    ["../escape", "..", "/etc/passwd", "a/b", "with space", "\x00null"],
)
def test_a_hostile_session_identifier_stays_inside_the_spool_directory(
    hostile: str,
) -> None:
    spool.append(hostile, _entry())

    written = list(spool.root_path().iterdir())
    assert len(written) == 1
    assert written[0].parent == spool.root_path()
    assert spool.entries(hostile) == [_entry()]


def test_two_sessions_do_not_mix() -> None:
    spool.append(SESSION, _entry(tool_name="Read"))
    spool.append("other-session", _entry(tool_name="Grep"))

    assert [record["tool_name"] for record in spool.entries(SESSION)] == ["Read"]
    assert [record["tool_name"] for record in spool.entries("other-session")] == [
        "Grep"
    ]


def test_the_spool_is_private_to_its_owner() -> None:
    spool.append(SESSION, _entry())

    root = spool.root_path()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for path in root.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_directory_other_users_can_read_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "spools"
    root.mkdir(mode=0o755, parents=True)

    with pytest.raises(spool.SpoolError):
        spool.append(SESSION, _entry())


def test_a_symlinked_spool_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "spools"
    root.mkdir(mode=0o700, parents=True)
    target = tmp_path / "stolen.jsonl"
    target.write_text("", encoding="utf-8")
    name = f"{spool.session_key(SESSION)}.jsonl"
    (root / name).symlink_to(target)

    with pytest.raises(spool.SpoolError):
        spool.append(SESSION, _entry())
    assert target.read_text() == ""


def test_a_full_spool_reports_rather_than_growing() -> None:
    entry = _entry()
    line = len(json.dumps(entry, ensure_ascii=False, sort_keys=True).encode()) + 1
    accepted = 0
    for _ in range(spool.MAX_SPOOL_BYTES // line + 2):
        if spool.append(SESSION, entry):
            accepted += 1
        else:
            break

    assert accepted > 0
    assert spool.append(SESSION, entry) is False
    size = next(spool.root_path().glob("*.jsonl")).stat().st_size
    assert size <= spool.MAX_SPOOL_BYTES


def test_an_oversized_record_is_refused_outright() -> None:
    with pytest.raises(spool.SpoolError):
        spool.append(SESSION, _entry(target="x" * spool.MAX_ENTRY_BYTES))


def test_lines_that_are_not_records_are_skipped() -> None:
    spool.append(SESSION, _entry())
    path = next(spool.root_path().glob("*.jsonl"))
    with path.open("a", encoding="utf-8") as stream:
        stream.write("not json\n[]\n\n")
    spool.append(SESSION, _entry(tool_name="Grep"))

    assert [record["tool_name"] for record in spool.entries(SESSION)] == [
        "Read",
        "Grep",
    ]


def test_an_absent_session_reads_as_empty() -> None:
    assert spool.entries("never-seen") == []
    assert spool.summarized("never-seen") == 0
    assert spool.newest() == ""


def test_the_summarized_mark_survives_between_processes() -> None:
    spool.append(SESSION, _entry())
    assert spool.summarized(SESSION) == 0

    spool.mark_summarized(SESSION, 1)
    assert spool.summarized(SESSION) == 1


def test_a_corrupt_mark_reads_as_nothing_shown() -> None:
    spool.append(SESSION, _entry())
    spool.mark_summarized(SESSION, 1)
    mark = next(spool.root_path().glob("*.mark"))
    mark.write_text("garbage", encoding="utf-8")

    assert spool.summarized(SESSION) == 0


def test_clearing_removes_the_session_entirely() -> None:
    spool.append(SESSION, _entry())
    spool.mark_summarized(SESSION, 1)

    spool.clear(SESSION)

    assert spool.entries(SESSION) == []
    assert spool.summarized(SESSION) == 0
    assert list(spool.root_path().iterdir()) == []
    spool.clear(SESSION)


def test_newest_finds_the_most_recently_written_session() -> None:
    spool.append("older", _entry())
    older = next(spool.root_path().glob("*.jsonl"))
    os.utime(older, (1, 1))
    spool.append(SESSION, _entry())

    stem = spool.newest()
    assert stem == spool.session_key(SESSION)
    assert spool.entries_for_stem(stem) == [_entry()]


@pytest.mark.parametrize("stem", ["../escape", "a/b", "with space", ""])
def test_a_stem_that_is_not_a_bare_name_is_refused(stem: str) -> None:
    with pytest.raises(spool.SpoolError):
        spool.entries_for_stem(stem)


@pytest.mark.parametrize("configured", ["relative/path", "/tmp/../etc"])
def test_a_configured_directory_that_is_not_a_plain_path_is_refused(
    configured: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", configured)

    with pytest.raises(spool.SpoolError):
        spool.root_path()


def test_the_largest_record_this_code_can_produce_fits_the_entry_cap() -> None:
    from shim_cli.guard import ENTITY_TYPES
    from shim_cli.session.record import Record

    worst = Record(
        client="claude",
        event="PostToolUseFailure",
        tool_name="mcp__some_server__a_very_long_tool_name_indeed",
        direction="inbound",
        mode="enforce",
        action="mask",
        entities=tuple((entity, 999999) for entity in ENTITY_TYPES),
        target="x" * 121,
        in_bytes=10**9,
        out_bytes=10**9,
        fields=99999,
        note="payload exceeds the traversal bound of 1000000 bytes",
    )
    entry = worst.as_dict()
    entry.update(
        session_id=spool.session_key("s" * 10_000),
        latency_ms=99999,
        ts="2026-08-29T14:51:06Z",
    )

    assert spool.append(SESSION, entry) is True
    assert len(json.dumps(entry, ensure_ascii=False, sort_keys=True)) < (
        spool.MAX_ENTRY_BYTES // 2
    )


def test_concurrent_hook_processes_do_not_lose_or_tear_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import sys

    root = Path(__file__).parents[2]
    payload = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "session_id": SESSION,
            "tool_name": "Read",
            "tool_input": {"file_path": "/work/service/.env"},
            "tool_response": {
                "type": "text",
                "file": {"content": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"},
            },
        }
    ).encode()
    workers = 12

    processes = [
        subprocess.Popen(
            (sys.executable, "-I", "-B", "-m", "shim_cli.hook", "claude"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=root,
            env={
                **os.environ,
                "PYTHONPATH": str(root / "src"),
                "SHIM_GUARD_SESSION_DIR": str(tmp_path / "spools"),
            },
        )
        for _ in range(workers)
    ]
    for process in processes:
        process.communicate(input=payload, timeout=120)
    assert {process.returncode for process in processes} == {0}

    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", str(tmp_path / "spools"))
    raw = next(spool.root_path().glob("*.jsonl")).read_text(encoding="utf-8")
    written = [line for line in raw.splitlines() if line.strip()]

    assert len(written) == workers, "a concurrent append was lost"
    assert len(spool.entries(SESSION)) == workers, "a concurrent append was torn"


def test_a_full_spool_says_so_to_the_reader(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", str(tmp_path / "spools"))
    monkeypatch.setattr(spool, "MAX_SPOOL_BYTES", 4_000)
    monkeypatch.setattr(spool, "MAX_ENTRY_BYTES", 400)

    assert spool.capped(SESSION) is False
    while spool.append(SESSION, _entry()):
        pass

    assert spool.capped(SESSION) is True
    stem = spool.newest()
    assert spool.capped_for_stem(stem) is True


def test_an_untouched_spool_is_not_capped(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", str(tmp_path / "spools"))

    assert spool.capped("never-seen") is False
