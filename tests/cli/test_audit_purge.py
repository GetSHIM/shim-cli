from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shim_cli.cli import audit
from shim_cli.cli.app import app

runner = CliRunner()
SENT = "eeee0000-0000-4000-8000-000000000001"
WRITTEN = "eeee0000-0000-4000-8000-000000000002"
MASKED = "eeee0000-0000-4000-8000-000000000003"
CLEAN = "eeee0000-0000-4000-8000-000000000004"
OTHER = "eeee0000-0000-4000-8000-000000000005"
DAY = "2026-09-28T12:00:00.000Z"
ENDING = (
    "deleted 1 session (1 file, 1 directory) and 2 lines of history.jsonl, "
    "on this computer only."
)


def _line(record: dict) -> bytes:
    return json.dumps(record).encode() + b"\n"


def _session(path: Path, *records: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(_line(record) for record in records))
    return path


def _user(content: object) -> dict:
    return {
        "type": "user",
        "cwd": "/work/kasa",
        "timestamp": DAY,
        "message": {"role": "user", "content": content},
    }


def _assistant(*blocks: dict) -> dict:
    return {
        "type": "assistant",
        "cwd": "/work/kasa",
        "timestamp": DAY,
        "message": {"role": "assistant", "content": list(blocks)},
    }


def _history_line(session: str, display: str) -> bytes:
    return _line(
        {
            "display": display,
            "pastedContents": {},
            "project": "/work/kasa",
            "sessionId": session,
            "timestamp": 1790000000000,
        }
    )


HISTORY_KEPT = (
    _history_line(CLEAN, "hello"),
    b"not json\n",
    _line({"display": "weird", "sessionId": ["a", "b"]}),
    _history_line(WRITTEN, "last")[:-1],
)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    projects = config / "projects" / "-work-kasa"
    _session(projects / f"{SENT}.jsonl", _user("mail alice@example.com"))
    _session(
        projects / SENT / "subagents" / "agent-1.jsonl",
        _assistant({"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}),
        _user(
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "TOKEN=0123456789abcdef-synthetic",
                }
            ]
        ),
    )
    (projects / SENT / "tool-results").mkdir()
    (projects / SENT / "tool-results" / "result.txt").write_text("kept by the client")
    outside = tmp_path / "outside" / "target.txt"
    outside.parent.mkdir()
    outside.write_text("not under the history")
    (projects / SENT / "link").symlink_to(outside)
    _session(
        projects / f"{WRITTEN}.jsonl",
        _assistant({"type": "text", "text": "I wrote dave@example.com"}),
    )
    _session(
        projects / f"{MASKED}.jsonl",
        _assistant({"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {}}),
        _user(
            [{"type": "tool_result", "tool_use_id": "toolu_2", "content": "<SECRET_1>"}]
        ),
    )
    _session(projects / f"{CLEAN}.jsonl", _user("hello"))
    history = config / "history.jsonl"
    history.write_bytes(
        _history_line(SENT, "mail alice@example.com")
        + HISTORY_KEPT[0]
        + HISTORY_KEPT[1]
        + _history_line(SENT, "second")
        + HISTORY_KEPT[2]
        + HISTORY_KEPT[3]
    )
    os.chmod(history, 0o644)
    monkeypatch.setattr(audit, "_terminal", lambda: True)
    return config


def _snapshot(root: Path) -> dict:
    return {
        path: path.read_bytes() if path.is_file() and not path.is_symlink() else None
        for path in sorted(root.rglob("*"))
    }


def test_the_exact_confirmation_deletes_only_the_session_that_sent_something(
    tree: Path, tmp_path: Path
) -> None:
    projects = tree / "projects" / "-work-kasa"
    before = _snapshot(tree)

    result = runner.invoke(app, ["audit", "--purge"], input="delete 1\n")

    assert result.exit_code == 1
    assert 'Type "delete 1" to continue:' in result.stdout
    assert result.stdout.rstrip().endswith(ENDING)
    assert not (projects / f"{SENT}.jsonl").exists()
    assert not (projects / SENT).exists()
    assert (tmp_path / "outside" / "target.txt").read_text() == "not under the history"
    after = _snapshot(tree)
    history = tree / "history.jsonl"
    assert history.read_bytes() == b"".join(HISTORY_KEPT)
    assert stat.S_IMODE(history.stat().st_mode) == 0o644
    assert {
        path: content
        for path, content in before.items()
        if path != history and SENT not in str(path)
    } == {path: content for path, content in after.items() if path != history}


def test_the_report_and_the_list_come_before_the_question(tree: Path) -> None:
    result = runner.invoke(app, ["audit", "--purge"], input="\n")
    lines = result.stdout.splitlines()

    assert "Nothing was changed, written or sent." not in result.stdout
    assert lines[0].startswith("shim audit — Claude Code history, 4 sessions")
    assert "sessions that sent something to the model" in lines
    listed = lines[lines.index("sessions that sent something to the model") + 1]
    assert listed.startswith("  /work/kasa                 2026-09-2")
    assert listed.endswith("  EMAIL 1, SECRET 1")
    assert lines[lines.index(listed) + 2 :][:2] == [
        "Delete this session from this computer? This cannot be undone.",
        "Close Claude Code first. What was already sent to the model provider is "
        "not affected.",
    ]
    assert "alice@example.com" not in result.stdout
    assert "agent-1" not in result.stdout


@pytest.mark.parametrize(
    "answer", ("delete 2\n", "DELETE 1\n", " delete 1\n", "\n", "")
)
def test_any_other_answer_deletes_nothing(tree: Path, answer: str) -> None:
    before = _snapshot(tree)

    result = runner.invoke(app, ["audit", "--purge"], input=answer)

    assert result.exit_code == 1
    assert result.stdout.rstrip().endswith("shim: nothing was deleted.")
    assert _snapshot(tree) == before


def test_purge_needs_a_terminal_and_stops_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "missing"))

    result = runner.invoke(app, ["audit", "--purge"], input="delete 1\n")

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "shim: --purge needs a terminal; nothing was deleted.\n"


def test_purge_cannot_be_combined_with_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(audit, "_terminal", lambda: True)

    result = runner.invoke(app, ["audit", "--purge", "--json"], input="delete 1\n")

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == (
        "shim: --purge cannot be combined with --json; nothing was deleted.\n"
    )


@pytest.mark.parametrize("changed", ("transcript", "sub-agent"))
def test_a_session_changed_after_the_scan_is_skipped_and_named(
    tree: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    transcript = tree / "projects" / "-work-kasa" / f"{SENT}.jsonl"
    agent = transcript.parent / SENT / "subagents" / "agent-1.jsonl"
    history = (tree / "history.jsonl").read_bytes()

    def confirm(_prompt: str) -> str:
        with (transcript if changed == "transcript" else agent).open("ab") as stream:
            stream.write(_line(_user("one more")))
        return "delete 1"

    monkeypatch.setattr(audit, "input", confirm, raising=False)

    result = runner.invoke(app, ["audit", "--purge"])

    assert transcript.exists()
    assert agent.exists()
    assert "/work/kasa 2026-09-2" in result.stdout
    assert "changed during the run, not deleted" in result.stdout
    assert result.stdout.rstrip().endswith(
        "deleted 0 sessions (0 files, 0 directories) and 0 lines of history.jsonl, "
        "on this computer only."
    )
    assert (tree / "history.jsonl").read_bytes() == history


def test_history_changed_while_writing_deletes_nothing(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = tree / "history.jsonl"
    before = _snapshot(tree)
    typed = b"\n" + _history_line(CLEAN, "typed meanwhile")
    real = audit.tempfile.mkstemp

    def racing(*arguments, **options):
        with history.open("ab") as stream:
            stream.write(typed)
        return real(*arguments, **options)

    monkeypatch.setattr(audit.tempfile, "mkstemp", racing)

    result = runner.invoke(app, ["audit", "--purge"], input="delete 1\n")

    assert result.stdout.rstrip().endswith(
        "shim: history.jsonl changed while shim was writing; nothing was deleted. "
        "Close Claude Code and run the command again."
    )
    assert _snapshot(tree) == {**before, history: before[history] + typed}


def test_history_that_cannot_be_rewritten_deletes_nothing(tree: Path) -> None:
    before = _snapshot(tree)
    os.chmod(tree, 0o500)
    try:
        result = runner.invoke(app, ["audit", "--purge"], input="delete 1\n")
    finally:
        os.chmod(tree, 0o700)

    assert result.exit_code == 1
    assert result.stdout.rstrip().endswith(
        "shim: history.jsonl could not be rewritten (Permission denied); "
        "nothing was deleted."
    )
    assert _snapshot(tree) == before


def test_a_folder_that_cannot_be_removed_keeps_its_transcript(tree: Path) -> None:
    transcript = tree / "projects" / "-work-kasa" / f"{SENT}.jsonl"
    locked = transcript.parent / SENT / "tool-results"
    os.chmod(locked, 0o500)
    try:
        result = runner.invoke(app, ["audit", "--purge"], input="delete 1\n")
    finally:
        os.chmod(locked, 0o700)

    assert transcript.exists()
    assert (
        "its folder could not be fully deleted, so its transcript was kept"
        in result.stdout
    )
    assert result.stdout.rstrip().endswith(
        "deleted 0 sessions (0 files, 0 directories) and 2 lines of history.jsonl, "
        "on this computer only."
    )
    assert (tree / "history.jsonl").read_bytes() == b"".join(HISTORY_KEPT)


def test_without_a_session_that_sent_something_nothing_is_asked(
    tree: Path,
) -> None:
    projects = tree / "projects" / "-work-kasa"
    (projects / f"{SENT}.jsonl").unlink()

    result = runner.invoke(app, ["audit", "--purge"], input="delete 1\n")

    assert result.exit_code == 0
    assert "Type" not in result.stdout
    assert result.stdout.rstrip().endswith(
        "shim: no session sent anything to the model; nothing was deleted."
    )


@pytest.mark.parametrize("name", (".jsonl", "..jsonl", "...jsonl"))
def test_a_file_named_only_dots_and_jsonl_is_not_a_session(
    tree: Path, name: str
) -> None:
    stray = _session(
        tree / "projects" / "-work-kasa" / name, _user("mail stray@example.com")
    )
    site = tree / "projects" / "-work-site"
    other = _session(site / f"{OTHER}.jsonl", {**_user("hello"), "cwd": "/work/site"})
    result_file = site / OTHER / "tool-results" / "result.txt"
    result_file.parent.mkdir(parents=True)
    result_file.write_text("kept")

    result = runner.invoke(app, ["audit", "--purge"], input="delete 1\n")

    assert result.stdout.rstrip().endswith(ENDING)
    assert stray.exists()
    assert other.exists()
    assert result_file.read_text() == "kept"
    assert (tree / "history.jsonl").read_bytes() == b"".join(HISTORY_KEPT)
