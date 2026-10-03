from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shim_cli.cli import audit
from shim_cli.cli.app import app

runner = CliRunner()
KASA = "aaaa1111-0000-4000-8000-000000000001"
OTHER = "bbbb2222-0000-4000-8000-000000000002"
DAY = "2026-09-28T12:00:00.000Z"
EARLY = "2026-09-01T12:00:00.000Z"
VALUES = (
    "alice@example.com",
    "bob@example.com",
    "carol@example.com",
    "dave@example.com",
    "erin@example.com",
    "frank@example.com",
    "grace@example.com",
    "henry@example.com",
    "kim@example.com",
    "ops@example.com",
    "0123456789abcdef",
    "AKIAIOSFODNN7EXAMPLE",
    "synthetic-password",
)


def _write(path: Path, records: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            record if isinstance(record, bytes) else json.dumps(record).encode() + b"\n"
            for record in records
        )
    )
    return path


def _user(content: object, cwd: str = "/work/kasa", when: str = DAY, **extra) -> dict:
    return {
        "type": "user",
        "cwd": cwd,
        "timestamp": when,
        "message": {"role": "user", "content": content},
        **extra,
    }


def _assistant(*blocks: dict, cwd: str = "/work/kasa", when: str = DAY) -> dict:
    return {
        "type": "assistant",
        "cwd": cwd,
        "timestamp": when,
        "message": {"role": "assistant", "content": list(blocks)},
    }


def _attachment(attachment: dict, cwd: str = "/work/kasa", when: str = DAY) -> dict:
    return {
        "type": "attachment",
        "cwd": cwd,
        "timestamp": when,
        "attachment": attachment,
    }


def _tool(identifier: str, name: str) -> dict:
    return {"type": "tool_use", "id": identifier, "name": name, "input": {}}


def _result(identifier: str, content: object, **extra) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": identifier,
        "content": content,
        **extra,
    }


def _kasa_records() -> list:
    return [
        _user("mail alice@example.com the totals"),
        _assistant(
            _tool("toolu_1", "Read"),
            _tool("toolu_2", "Bash"),
            {"type": "text", "text": "I will check dave@example.com"},
            {"type": "thinking", "thinking": "maybe erin@example.com"},
        ),
        _user(
            [
                _result(
                    "toolu_1",
                    [
                        {
                            "type": "text",
                            "text": "API_TOKEN=0123456789abcdef-synthetic\n",
                        }
                    ],
                )
            ]
        ),
        _user(
            [
                _result(
                    "toolu_2",
                    "Exit code 1\nAWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
                    is_error=True,
                )
            ]
        ),
        _attachment(
            {
                "type": "file",
                "filename": "/work/kasa/.env",
                "displayPath": ".env",
                "content": {
                    "type": "text",
                    "file": {
                        "filePath": "/work/kasa/.env",
                        "content": "SUPPORT_EMAIL=ops@example.com\n",
                        "numLines": 1,
                        "startLine": 1,
                        "totalLines": 1,
                    },
                },
            }
        ),
        _attachment(
            {
                "type": "file",
                "filename": "/work/kasa/notes.txt",
                "displayPath": "notes.txt",
                "content": "ask bob@example.com",
            }
        ),
        _attachment(
            {
                "type": "queued_command",
                "commandMode": "prompt",
                "prompt": [{"type": "text", "text": "also carol@example.com"}],
            }
        ),
        _attachment(
            {
                "type": "edited_text_file",
                "filename": "/work/kasa/config.py",
                "snippet": "DB_PASSWORD = 'synthetic-password-1'",
            }
        ),
        _attachment(
            {"type": "hook_additional_context", "content": ["kim@example.com"]}
        ),
        _assistant(_tool("toolu_3", "Read")),
        _user([_result("toolu_3", "<SECRET_1> and <EMAIL_2>")]),
        _user("summary mentions frank@example.com", isCompactSummary=True),
        b"not json\n",
        b"[1, 2]\n",
        b"\xff\xfe broken\n",
        b"x" * 9_000_000 + b"\n",
        {
            "type": "user",
            "cwd": "/work/kasa",
            "timestamp": DAY,
            "message": {"content": 42},
        },
    ]


def _subagent_records() -> list:
    return [
        _user("look up grace@example.com"),
        _assistant(_tool("toolu_9", "Grep")),
        _user([_result("toolu_9", "TOKEN=0123456789abcdef-synthetic")]),
    ]


@pytest.fixture
def history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    times = iter([0.0])
    monkeypatch.setattr(audit, "perf_counter", lambda: next(times, 0.5))
    projects = config / "projects"
    _write(projects / "-work-kasa" / f"{KASA}.jsonl", _kasa_records())
    _write(
        projects / "-work-kasa" / KASA / "subagents" / "agent-1.jsonl",
        _subagent_records(),
    )
    other = _write(
        projects / "-work-other" / f"{OTHER}.jsonl",
        [_user("write to henry@example.com", cwd="/work/other", when=EARLY)],
    )
    early = 1_788_264_000
    os.utime(other, (early, early))
    return projects


REPORT = """\
shim audit — Claude Code history, 2 sessions in 2 projects, 2026-09-01 to 2026-09-28

reached the model
  EMAIL            5  in 2 sessions   your prompt 3 · @.env 1 · @notes.txt 1
  SECRET           4  in 1 session   Grep 1 · Read 1 · edited config.py 1 · +1 more

by project
  /work/kasa                 EMAIL 4, SECRET 4 · last 2026-09-28
  /work/other                EMAIL 1 · last 2026-09-01

already masked by shim       2 values in 1 session
model output                 2 EMAIL (written by the model; it may repeat values it was given)

skipped 4 lines that could not be read.
scanned 2 sessions, 9.0 MB, in 0.5 s. Nothing was changed, written or sent.
"""


def test_the_report_counts_each_door_apart(history: Path) -> None:
    result = runner.invoke(app, ["audit"])

    assert result.exit_code == 1
    assert result.stdout == REPORT


def test_since_drops_earlier_records_and_older_files(history: Path) -> None:
    result = runner.invoke(app, ["audit", "--since", "2026-09-15"])

    assert result.exit_code == 1
    assert result.stdout.splitlines()[0] == (
        "shim audit — Claude Code history, 1 session in 1 project, "
        "2026-09-28 to 2026-09-28"
    )
    assert "  EMAIL            4  in 1 session   your prompt 2" in result.stdout
    assert "/work/other" not in result.stdout


def test_since_skips_records_before_it_inside_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    _write(
        tmp_path / "claude" / "projects" / "-work-kasa" / f"{KASA}.jsonl",
        [
            _user("old alice@example.com", when=EARLY),
            _user("new token=0123456789abcdef-synthetic"),
        ],
    )

    result = runner.invoke(app, ["audit", "--since", "2026-09-15", "--json"])

    assert json.loads(result.stdout)["reached"] == {
        "SECRET": {"doors": {"your prompt": 1}, "sessions": 1, "total": 1}
    }


def test_project_keeps_the_path_and_below_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    projects = tmp_path / "claude" / "projects"
    for index, cwd in enumerate(("/work/kasa", "/work/kasa/api", "/work/kasa-old")):
        _write(
            projects / f"-p{index}" / f"{index:08d}-0000-4000-8000-000000000000.jsonl",
            [_user("mail alice@example.com", cwd=cwd)],
        )

    result = runner.invoke(app, ["audit", "--project", "/work/kasa", "--json"])
    payload = json.loads(result.stdout)

    assert sorted(payload["by_project"]) == ["/work/kasa", "/work/kasa/api"]
    assert payload["sessions"] == 2


def test_the_json_report_carries_every_count(history: Path) -> None:
    result = runner.invoke(app, ["audit", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert {
        key: payload[key]
        for key in ("schema_version", "command", "status", "sessions", "projects")
    } == {
        "schema_version": 1,
        "command": "audit",
        "status": "findings",
        "sessions": 2,
        "projects": 2,
    }
    assert payload["reached"]["SECRET"] == {
        "total": 4,
        "sessions": 1,
        "doors": {"Grep": 1, "Read": 1, "edited config.py": 1, "failed Bash": 1},
    }
    assert payload["by_project"]["/work/other"] == {
        "counts": {"EMAIL": 1},
        "last": "2026-09-01",
    }
    assert payload["by_session"][KASA] == {
        "project": "/work/kasa",
        "last": "2026-09-28",
        "reached": {"EMAIL": 4, "SECRET": 4},
        "model_output": {"EMAIL": 2},
        "masked": 2,
    }
    assert payload["masked"] == {"values": 2, "sessions": 1}
    assert payload["model_output"] == {"EMAIL": 2}
    assert payload["scanned"] == {
        "sessions": 2,
        "bytes": payload["scanned"]["bytes"],
        "seconds": 0.5,
        "skipped_lines": 4,
        "uninspected_texts": 0,
    }


@pytest.mark.parametrize("arguments", (["audit"], ["audit", "--json"]))
def test_no_value_reaches_the_output(history: Path, arguments: list) -> None:
    result = runner.invoke(app, arguments)
    output = result.stdout + result.stderr

    assert [value for value in VALUES if value in output] == []


def test_the_history_is_left_exactly_as_it_was(history: Path) -> None:
    def snapshot(root: Path) -> dict:
        return {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*"))
        }

    before = snapshot(history.parent)
    temporary = set(Path(tempfile.gettempdir()).rglob("*"))

    runner.invoke(app, ["audit"])

    assert snapshot(history.parent) == before
    assert set(Path(tempfile.gettempdir()).rglob("*")) == temporary


def test_a_missing_history_exits_2_with_one_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "none"))

    result = runner.invoke(app, ["audit"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == (
        f"shim: no Claude Code history at {tmp_path / 'none' / 'projects'}.\n"
    )


def test_an_empty_history_says_so_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "claude" / "projects").mkdir(parents=True)

    result = runner.invoke(app, ["audit"])

    assert result.exit_code == 0
    assert result.stdout == "shim: there are no Claude Code sessions to audit.\n"


def test_without_claude_config_dir_the_home_history_is_read(tmp_path: Path) -> None:
    _write(
        Path.home() / ".claude" / "projects" / "-work-kasa" / f"{KASA}.jsonl",
        [_user("mail alice@example.com")],
    )

    result = runner.invoke(app, ["audit", "--json"])

    assert json.loads(result.stdout)["reached"]["EMAIL"]["total"] == 1


def test_no_symlink_is_followed_out_of_the_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    projects = tmp_path / "claude" / "projects"
    outside = _write(
        tmp_path / "outside" / f"{OTHER}.jsonl", [_user("mail alice@example.com")]
    )
    (projects / "-linked-file").mkdir(parents=True)
    (projects / "-linked-file" / f"{KASA}.jsonl").symlink_to(outside)
    (projects / "-linked-folder").symlink_to(outside.parent, target_is_directory=True)

    result = runner.invoke(app, ["audit"])

    assert result.exit_code == 0
    assert result.stdout == "shim: there are no Claude Code sessions to audit.\n"


def test_text_the_detector_cannot_analyse_is_counted_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    _write(
        tmp_path / "claude" / "projects" / "-work-kasa" / f"{KASA}.jsonl",
        [_user("\ufdfa" * 12_000), _user("mail alice@example.com")],
    )

    result = runner.invoke(app, ["audit", "--json"])
    payload = json.loads(result.stdout)

    assert payload["scanned"]["uninspected_texts"] == 1
    assert payload["reached"]["EMAIL"]["total"] == 1


def test_a_small_history_is_measured_in_kilobytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    transcript = _write(
        tmp_path / "claude" / "projects" / "-work-kasa" / f"{KASA}.jsonl",
        [_user("hello")],
    )

    result = runner.invoke(app, ["audit"])

    assert f"scanned 1 session, {-(-transcript.stat().st_size // 1000)} KB, in" in (
        result.stdout
    )


def test_a_malformed_since_exits_2_with_one_sentence(history: Path) -> None:
    result = runner.invoke(app, ["audit", "--since", "28/09/2026"])

    assert result.exit_code == 2
    assert result.stderr == "shim: --since takes a date as YYYY-MM-DD.\n"


def test_a_value_in_a_tool_name_or_a_field_that_is_not_text_is_not_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    hidden = {"note": "token=0123456789abcdef-synthetic"}
    _write(
        tmp_path / "claude" / "projects" / "-work-kasa" / f"{KASA}.jsonl",
        [
            _assistant(
                _tool("toolu_1", "mcp__AKIAIOSFODNN7EXAMPLE__query"),
                {"type": "tool_use", "id": "toolu_2", "name": hidden, "input": {}},
            ),
            _user([_result("toolu_1", "bob@example.com")]),
            _user([_result("toolu_2", "carol@example.com")]),
            _attachment({"type": "file", "displayPath": hidden, "content": "x@y.io"}),
            _attachment(
                {"type": "edited_text_file", "filename": [hidden], "snippet": "z@y.io"}
            ),
        ],
    )

    for arguments in (["audit"], ["audit", "--json"]):
        output = runner.invoke(app, arguments).stdout
        assert "mcp__<SECRET_1>__query" in output
        assert "AKIAIOSFODNN7EXAMPLE" not in output
        assert "0123456789abcdef" not in output
        assert "note" not in output


@pytest.mark.parametrize("arguments", (["audit"], ["audit", "--json"]))
def test_settings_that_cannot_be_read_exit_2_with_the_reason(
    history: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list,
) -> None:
    settings = tmp_path / "shim.toml"
    settings.write_text("enabled_entities = [\n", encoding="utf-8")
    monkeypatch.setenv("SHIM_CONFIG", str(settings))

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    reason = f"Settings at {settings} are invalid: "
    if "--json" in arguments:
        assert json.loads(result.stdout)["error"].startswith(reason)
    else:
        assert result.stdout == ""
        assert result.stderr.startswith(f"shim: {reason}")


def test_a_timestamp_that_cannot_be_shown_is_ignored_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    _write(
        tmp_path / "claude" / "projects" / "-work-kasa" / f"{KASA}.jsonl",
        [
            _user("mail alice@example.com", when="9999-12-31T23:59:59.000-14:00"),
            _user("mail bob@example.com"),
        ],
    )

    result = runner.invoke(app, ["audit", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["reached"]["EMAIL"]["total"] == 2
