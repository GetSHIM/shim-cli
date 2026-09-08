from __future__ import annotations

import json
from pathlib import Path

import pytest
from click import unstyle
from typer.testing import CliRunner

from shim_cli.cli.app import app
from shim_cli.config import config_path, legacy_config_path, load_policy
from shim_cli.session import ledger

runner = CliRunner()
SETTINGS = 'enabled_entities = ["EMAIL"]\n'


def _says(result, phrase: str) -> bool:
    """Rich wraps to the terminal width, so whitespace carries no meaning."""
    return "".join(phrase.split()) in "".join(unstyle(result.output).split())


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".state"))
    monkeypatch.delenv("SHIM_CONFIG", raising=False)
    monkeypatch.delenv("SHIM_GUARD_CONFIG", raising=False)
    monkeypatch.delenv("SHIM_GUARD_STATE_DIR", raising=False)
    return home


def _write_legacy_settings(body: str = SETTINGS) -> Path:
    legacy = legacy_config_path()
    assert legacy is not None
    legacy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    legacy.write_text(body, encoding="utf-8")
    return legacy


def _write_legacy_ledger(*names: str) -> Path:
    directory = ledger.legacy_root_path()
    assert directory is not None
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text('{"session_id":"s"}\n', encoding="utf-8")
    return directory


def test_the_hook_reads_the_old_settings_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    legacy = _write_legacy_settings()
    before = sorted(p.name for p in legacy.parent.parent.iterdir())

    assert load_policy().entities == ("EMAIL",)

    assert legacy.is_file()
    assert not config_path().exists()
    assert sorted(p.name for p in legacy.parent.parent.iterdir()) == before


def test_the_new_settings_file_wins_and_nothing_moves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    legacy = _write_legacy_settings()
    target = config_path()
    target.parent.mkdir(mode=0o700, parents=True)
    target.write_text('enabled_entities = ["IBAN"]\n', encoding="utf-8")

    result = runner.invoke(app, ["config"])

    assert result.exit_code == 0
    assert not _says(result, "moved settings to")
    assert legacy.is_file()
    assert load_policy().entities == ("IBAN",)


def test_config_moves_the_settings_file_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    legacy = _write_legacy_settings()

    first = runner.invoke(app, ["config"])
    second = runner.invoke(app, ["config"])

    assert first.exit_code == 0
    assert _says(first, f"moved settings to {config_path()}")
    assert not _says(second, "moved settings to")
    assert not legacy.exists()
    assert config_path().read_text(encoding="utf-8") == SETTINGS
    assert config_path().parent.stat().st_mode & 0o077 == 0


def test_report_moves_ledger_files_and_removes_the_empty_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    old = _write_legacy_ledger("ledger-2026-08.jsonl", "ledger-2026-09.jsonl")

    first = runner.invoke(app, ["report"])
    second = runner.invoke(app, ["report"])

    assert _says(first, "moved 2 ledger files to")
    assert not _says(second, "moved")
    assert not old.exists()
    assert sorted(p.name for p in ledger.root_path().iterdir()) == [
        "ledger-2026-08.jsonl",
        "ledger-2026-09.jsonl",
    ]


def test_a_name_collision_is_skipped_and_a_foreign_file_keeps_the_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    old = _write_legacy_ledger("ledger-2026-08.jsonl")
    (old / "notes.txt").write_text("someone else's file\n", encoding="utf-8")
    target = ledger.root_path()
    target.mkdir(mode=0o700, parents=True)
    (target / "ledger-2026-08.jsonl").write_text('{"session_id":"new"}\n', "utf-8")

    result = runner.invoke(app, ["report"])

    assert not _says(result, "moved")
    assert (old / "ledger-2026-08.jsonl").is_file()
    assert (old / "notes.txt").is_file()
    assert (target / "ledger-2026-08.jsonl").read_text(
        encoding="utf-8"
    ) == '{"session_id":"new"}\n'


def test_purge_empties_both_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    _write_legacy_ledger("ledger-2026-08.jsonl")

    result = runner.invoke(app, ["ledger", "purge", "--yes"])

    assert result.exit_code == 0
    assert ledger.files() == []
    legacy = ledger.legacy_root_path()
    assert legacy is not None and not legacy.exists()


def test_a_json_run_keeps_stdout_parseable_while_reporting_the_move(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    _write_legacy_ledger("ledger-2026-08.jsonl")

    result = runner.invoke(app, ["report", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["schema_version"]


def test_a_pinned_variable_bypasses_both_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    pinned = tmp_path / "pinned.toml"
    pinned.write_text('enabled_entities = ["IBAN"]\n', encoding="utf-8")
    monkeypatch.setenv("SHIM_CONFIG", str(pinned))

    assert legacy_config_path() is None
    assert config_path() == pinned
    assert load_policy().entities == ("IBAN",)


def _legacy_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A 0.2.0 machine: old settings, old ledger, old fragments, old hook file."""
    from shim_cli.clients.claude import settings as claude_settings
    from shim_cli.clients.codex import settings as codex_settings
    from shim_cli.clients.copilot import settings as copilot_settings
    from shim_cli.clients.hook_settings import add_groups

    home = _home(monkeypatch, tmp_path)
    _write_legacy_settings()
    _write_legacy_ledger("ledger-2026-08.jsonl")

    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_bytes(
        add_groups(
            json.dumps(
                {"model": "opus", "hooks": {"Notification": [_foreign()]}}
            ).encode(),
            claude_settings.legacy_hook_groups(),
        )
    )
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "hooks.json").write_bytes(
        add_groups(b"{}", codex_settings.legacy_hook_groups())
    )
    hooks = home / ".copilot" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "shim-guard.json").write_bytes(
        json.dumps(
            copilot_settings.hook_document(module=copilot_settings.LEGACY_HOOK_MODULE),
            ensure_ascii=False,
            indent=2,
        ).encode()
        + b"\n"
    )
    return home


def _shim_state(home: Path) -> dict[str, object]:
    """shim's own artefacts. doctor runs the client binary, which writes its own."""
    files = (
        ".config/shim-guard/config.toml",
        ".claude/settings.json",
        ".codex/hooks.json",
        ".copilot/hooks/shim-guard.json",
        ".copilot/hooks/shim.json",
    )
    state: dict[str, object] = {
        name: (home / name).read_bytes() if (home / name).is_file() else None
        for name in files
    }
    ledger_dir = home / ".state" / "shim-guard"
    state["ledger"] = (
        sorted(p.name for p in ledger_dir.iterdir()) if ledger_dir.is_dir() else None
    )
    return state


def _foreign() -> dict:
    return {"hooks": [{"type": "command", "command": "/bin/true"}]}


@pytest.mark.parametrize("client", ("claude", "codex", "copilot"))
def test_install_replaces_a_020_fragment_in_place_and_is_idempotent(
    client: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shim_cli.cli.integrations import client_plan

    home = _legacy_home(monkeypatch, tmp_path)
    target = client_plan(client, "install").target

    first = runner.invoke(app, ["install", client, "--yes"])
    after_first = target.read_bytes()
    second = runner.invoke(app, ["install", client, "--yes"])

    assert (first.exit_code, second.exit_code) == (0, 0)
    assert b"shim_guard" not in after_first
    assert target.read_bytes() == after_first
    assert after_first.count(b"shim_cli.hook") == 1 if client != "claude" else True
    if client == "claude":
        document = json.loads(after_first)
        assert document["model"] == "opus"
        assert document["hooks"]["Notification"] == [_foreign()]
    if client == "copilot":
        assert not (home / ".copilot" / "hooks" / "shim-guard.json").exists()
        assert (home / ".copilot" / "hooks" / "shim.json").is_file()


@pytest.mark.parametrize("client", ("claude", "codex", "copilot"))
def test_revert_removes_either_shape_and_keeps_unrelated_hooks(
    client: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shim_cli.cli.integrations import client_plan

    home = _legacy_home(monkeypatch, tmp_path)
    target = client_plan(client, "install").target

    result = runner.invoke(app, ["revert", client, "--yes"])

    assert result.exit_code == 0
    if client == "copilot":
        assert not (home / ".copilot" / "hooks" / "shim-guard.json").exists()
        return
    body = target.read_bytes()
    assert b"shim_guard" not in body and b"shim_cli" not in body
    if client == "claude":
        document = json.loads(body)
        assert document["model"] == "opus"
        assert document["hooks"]["Notification"] == [_foreign()]


@pytest.mark.parametrize("client", ("claude", "codex", "copilot"))
def test_doctor_names_every_old_shape_and_changes_nothing(
    client: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _legacy_home(monkeypatch, tmp_path)
    before = _shim_state(home)

    document = runner.invoke(app, ["doctor", client, "--json"])
    text = runner.invoke(app, ["doctor", client])

    checks = {item["name"]: item for item in json.loads(document.output)["checks"]}
    assert checks["legacy_names"]["status"] == "WARN"
    assert _says(text, "settings are still at")
    assert _says(text, "ledger files are still in")
    if client == "copilot":
        # Copilot's old shape is the file name, not a fragment inside it.
        assert _says(text, "hook file uses the old name")
    else:
        assert _says(text, "the installed hook fragment names the old module")
    assert _says(text, f"run shim install {client}")
    assert _shim_state(home) == before
