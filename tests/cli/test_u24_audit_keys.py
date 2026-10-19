from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shim_cli.cli import audit
from shim_cli.cli.app import app

runner = CliRunner()
SESSION = "cccc3333-0000-4000-8000-000000000001"
DAY = "2026-09-28T12:00:00.000Z"
SECRET = "AKIAIOSFODNN7EXAMPLE"


def _transcript(config: Path, *lines: bytes) -> Path:
    path = config / "projects" / "-work-kasa" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"".join(lines))
    return path


def _user(text: str) -> bytes:
    record = {
        "type": "user",
        "cwd": "/work/kasa",
        "timestamp": DAY,
        "message": {"role": "user", "content": text},
    }
    return json.dumps(record).encode() + b"\n"


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(folder))
    monkeypatch.setattr(audit, "_terminal", lambda: True)
    return folder


def _prompt_line() -> bytes:
    record = {"display": f"deploy with {SECRET}", "sessionId": SESSION}
    return json.dumps(record).encode() + b"\n"


def test_purge_refuses_when_history_is_a_symlink_and_deletes_nothing(
    config: Path, tmp_path: Path
) -> None:
    transcript = _transcript(config, _user(f"key {SECRET}"))
    real = tmp_path / "dotfiles" / "history.jsonl"
    real.parent.mkdir()
    real.write_bytes(_prompt_line())
    (config / "history.jsonl").symlink_to(real)

    result = runner.invoke(app, ["audit", "--purge"], input="delete 1\n")

    assert "nothing was deleted" in result.stdout
    assert "deleted 1 session" not in result.stdout
    assert transcript.exists()
    assert real.read_bytes() == _prompt_line()


def test_purge_refuses_when_history_is_not_a_regular_file(config: Path) -> None:
    transcript = _transcript(config, _user(f"key {SECRET}"))
    (config / "history.jsonl").mkdir()

    result = runner.invoke(app, ["audit", "--purge"], input="delete 1\n")

    assert "nothing was deleted" in result.stdout
    assert transcript.exists()


def test_purge_with_no_history_file_still_deletes_the_transcript(config: Path) -> None:
    transcript = _transcript(config, _user(f"key {SECRET}"))

    result = runner.invoke(app, ["audit", "--purge"], input="delete 1\n")

    assert "deleted 1 session" in result.stdout
    assert not transcript.exists()


def _unread_line(config: Path) -> None:
    _transcript(config, _user("hello"), b"not json at all\n")


def test_skipped_lines_and_nothing_found_is_incomplete_not_safe(config: Path) -> None:
    _unread_line(config)

    result = runner.invoke(app, ["audit", "--json"])
    payload = json.loads(result.stdout)

    assert payload["status"] == "incomplete"
    assert payload["scanned"]["skipped_lines"] == 1
    assert result.exit_code == 1


def test_skipped_lines_and_nothing_found_exit_1_in_the_human_report(
    config: Path,
) -> None:
    _unread_line(config)

    result = runner.invoke(app, ["audit"])

    assert "skipped 1 line that could not be read" in result.stdout
    assert result.exit_code == 1


def test_uninspected_text_and_nothing_found_is_incomplete(
    config: Path, unanalysable: str
) -> None:
    _transcript(config, _user(unanalysable))

    result = runner.invoke(app, ["audit", "--json"])
    payload = json.loads(result.stdout)

    assert payload["scanned"]["uninspected_texts"] == 1
    assert payload["status"] == "incomplete"
    assert result.exit_code == 1


def test_a_complete_clean_scan_is_still_safe_and_exits_0(config: Path) -> None:
    _transcript(config, _user("hello"))

    result = runner.invoke(app, ["audit", "--json"])

    assert json.loads(result.stdout)["status"] == "safe"
    assert result.exit_code == 0


def test_an_unreadable_file_under_json_is_a_json_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads every file")
    path = tmp_path / "s.env"
    path.write_text(f"AWS_ACCESS_KEY_ID={SECRET}\n", encoding="utf-8")
    path.chmod(0)
    try:
        result = runner.invoke(app, ["keys", "--json", str(path)])
    finally:
        path.chmod(0o600)

    payload = json.loads(result.stdout)
    assert result.exit_code == 2
    assert payload["status"] == "error"
    assert payload["code"] == "FILE_UNREADABLE"
    assert SECRET not in result.stdout + result.stderr
