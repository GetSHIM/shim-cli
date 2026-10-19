"""install, doctor and config say what is true about the files they name (U21).

Every test runs in a temporary HOME with its own CLAUDE_CONFIG_DIR, SHIM_CONFIG
and XDG_CONFIG_HOME; nothing reads or writes a real client or shim settings file.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
from click import unstyle
from typer.testing import CliRunner

from shim_cli.cli import diagnostics, migration
from shim_cli.cli.app import app
from shim_cli.cli.resolution import installed_plugins
from shim_cli.clients.claude import settings as claude
from shim_cli.config import config_path, legacy_config_path

runner = CliRunner()
PLUGIN = {"version": 2, "plugins": {"shim-cli@shim-cli": [{"installPath": "/x"}]}}


def _flat(result) -> str:
    """Rich wraps to the terminal width, even inside a path: whitespace is noise."""
    return "".join(unstyle(result.output).split())


def _squeeze(text: str) -> str:
    return "".join(text.split())


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    return home


def _claude_settings(home: Path, mode: int = 0o600, body: bytes = b"{}") -> Path:
    target = home / ".claude" / "settings.json"
    target.parent.mkdir(mode=0o700, exist_ok=True)
    target.write_bytes(body)
    target.chmod(mode)
    return target


def _plugins(directory: Path, document: dict = PLUGIN) -> None:
    (directory / "plugins").mkdir(parents=True, exist_ok=True)
    (directory / "plugins" / "installed_plugins.json").write_text(
        json.dumps(document), encoding="utf-8"
    )


def _install_json(client: str = "claude") -> dict:
    result = runner.invoke(app, ["install", client, "--yes", "--json"])
    assert result.exit_code == 2, result.output
    return json.loads(result.stdout)


# F086: a refusal says what is wrong with the file, and how to mend that.


def test_a_group_writable_settings_file_is_mended_by_chmod_not_by_ownership(
    monkeypatch, tmp_path: Path
) -> None:
    target = _claude_settings(_home(monkeypatch, tmp_path), mode=0o664)

    error = _install_json()

    assert error["code"] == "CLIENT_SETTINGS_UNSAFE"
    assert "writable by another user" in error["error"]
    assert f"chmod g-w,o-w {shlex.quote(str(target))}" in error["fix"]
    assert "owned by you" not in error["fix"]


def test_the_human_refusal_prints_the_reason_and_the_fix(
    monkeypatch, tmp_path: Path
) -> None:
    target = _claude_settings(_home(monkeypatch, tmp_path), mode=0o664)

    result = runner.invoke(app, ["install", "claude", "--yes"])

    assert result.exit_code == 2
    text = _flat(result)
    assert _squeeze("writable by another user") in text
    assert _squeeze(f"chmod g-w,o-w {shlex.quote(str(target))}") in text


def test_a_group_writable_folder_is_named_by_its_fix(
    monkeypatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    target = _claude_settings(home)
    target.parent.chmod(0o770)

    error = _install_json()

    assert error["code"] == "CLIENT_SETTINGS_UNSAFE"
    assert "parent is writable by another user" in error["error"]
    assert f"chmod g-w,o-w {shlex.quote(str(target.parent))}" in error["fix"]


def test_a_relative_client_home_asks_for_an_absolute_one(
    monkeypatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("CODEX_HOME", "codex-dev")

    error = _install_json("codex")

    assert error["code"] == "CLIENT_SETTINGS_UNSAFE"
    assert "absolute" in error["error"]
    assert "absolute" in error["fix"]
    assert "owned by you" not in error["fix"]


def test_a_group_writable_ancestor_is_not_called_a_race(
    monkeypatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    home.chmod(0o775)

    error = _install_json()

    assert error["code"] == "CLIENT_SETTINGS_UNSAFE"
    assert "ancestor is writable by another user" in error["error"]
    assert "chmod g-w,o-w" in error["fix"]


def test_a_conflict_that_parses_is_not_told_to_fix_the_syntax(
    monkeypatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    document = json.loads(claude.add_hook(None))
    group = document["hooks"]["Stop"][0]
    group["hooks"].append({"type": "command", "command": "/usr/bin/true"})
    _claude_settings(home, body=json.dumps(document).encode())

    result = runner.invoke(app, ["revert", "claude", "--yes", "--json"])

    error = json.loads(result.stdout)
    assert error["code"] == "CLIENT_SETTINGS_MALFORMED"
    assert "beside another hook" in error["error"]
    assert "so it parses" not in error["fix"]
    assert "beside another hook" in error["fix"]


def test_a_conflict_that_does_not_parse_still_says_where(
    monkeypatch, tmp_path: Path
) -> None:
    _claude_settings(_home(monkeypatch, tmp_path), body=b'{"hooks": ')

    result = runner.invoke(app, ["install", "claude", "--yes", "--json"])

    error = json.loads(result.stdout)
    assert error["code"] == "CLIENT_SETTINGS_MALFORMED"
    assert "line 1" in error["error"]
    assert "so it parses" in error["fix"]


# F116 (+F087): the plugin is looked up where Claude Code keeps it.


def test_plugins_are_read_from_claude_config_dir(monkeypatch, tmp_path: Path) -> None:
    home = _home(monkeypatch, tmp_path)
    isolated = tmp_path / "isolated"
    _plugins(isolated)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolated))

    assert [p["key"] for p in installed_plugins()] == ["shim-cli@shim-cli"]
    assert not (home / ".claude").exists()


def test_the_home_argument_still_names_a_home_folder(tmp_path: Path) -> None:
    _plugins(tmp_path / ".claude")

    assert [p["key"] for p in installed_plugins(tmp_path)] == ["shim-cli@shim-cli"]


def test_a_plugin_switched_off_in_settings_is_not_installed(
    monkeypatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    document = {
        "version": 2,
        "plugins": {
            "shim-cli@shim-cli": [{"installPath": "/x"}],
            "shim-guard@shim-guard": [{"installPath": "/y"}],
        },
    }
    _plugins(home / ".claude", document)
    _claude_settings(
        home, body=json.dumps({"enabledPlugins": {"shim-cli@shim-cli": False}}).encode()
    )

    assert [p["key"] for p in installed_plugins()] == ["shim-guard@shim-guard"]


def test_doctor_sees_the_plugin_under_claude_config_dir(
    monkeypatch, tmp_path: Path
) -> None:
    isolated = tmp_path / "isolated"
    isolated.mkdir(mode=0o700)
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolated))
    _plugins(isolated)
    (isolated / "settings.json").write_bytes(claude.add_hook(None))
    (isolated / "settings.json").chmod(0o600)

    check = diagnostics._duplicate_check("claude", frozenset({"Stop"}))

    assert check.status == "FAIL"
    assert "inspected twice" in check.detail


# F115 (+F088): any number of plugin keys is a finding, never a traceback.


@pytest.mark.parametrize(
    "keys,uninstalled",
    [
        (["shim-cli@shim-cli", "shim-cli@my-fork"], ["shim-cli@my-fork"]),
        (["shim-guard@shim-guard", "shim-cli@shim-cli"], ["shim-guard@shim-guard"]),
        (
            ["shim-cli@shim-cli", "shim-cli@my-fork", "shim-cli@other"],
            ["shim-cli@my-fork", "shim-cli@other"],
        ),
    ],
)
def test_two_plugin_keys_are_named_and_one_is_kept(
    monkeypatch, tmp_path: Path, keys: list, uninstalled: list
) -> None:
    home = _home(monkeypatch, tmp_path)
    _plugins(
        home / ".claude",
        {"version": 2, "plugins": {key: [{"installPath": "/x"}] for key in keys}},
    )

    check = diagnostics._duplicate_check("claude", frozenset())

    assert check.status == "FAIL"
    assert check.code == "DUPLICATE_HOOKS"
    assert all(key in check.detail for key in keys)
    assert check.fix == "; ".join(
        f"claude plugin uninstall {key}" for key in uninstalled
    )


def test_doctor_json_survives_two_plugin_keys(monkeypatch, tmp_path: Path) -> None:
    home = _home(monkeypatch, tmp_path)
    _plugins(
        home / ".claude",
        {
            "version": 2,
            "plugins": {
                "shim-cli@shim-cli": [{"installPath": "/x"}],
                "shim-cli@my-fork": [{"installPath": "/y"}],
            },
        },
    )

    result = runner.invoke(app, ["doctor", "claude", "--json"])

    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    checks = {c["name"]: c for c in json.loads(result.stdout)["checks"]}
    assert checks["duplicate_hooks"]["code"] == "DUPLICATE_HOOKS"


# F089: a move that cannot be made is a warning, never a traceback.


def test_a_settings_move_that_fails_is_a_warning_note(
    monkeypatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    legacy = legacy_config_path()
    assert legacy is not None
    legacy.parent.mkdir(mode=0o700, parents=True)
    legacy.write_text('enabled_entities = ["EMAIL"]\n', encoding="utf-8")
    # A file where the folder belongs: no privilege tricks, so it holds as root.
    (home / ".config" / "shim").write_text("", encoding="utf-8")

    notes = migration.settings()

    assert len(notes) == 1
    assert notes[0].startswith("could not move settings")
    assert legacy.is_file()


def test_install_goes_on_when_the_settings_move_fails(
    monkeypatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    legacy = legacy_config_path()
    assert legacy is not None
    legacy.parent.mkdir(mode=0o700, parents=True)
    legacy.write_text('enabled_entities = ["EMAIL"]\n', encoding="utf-8")
    (home / ".config" / "shim").write_text("", encoding="utf-8")
    _claude_settings(home)

    result = runner.invoke(app, ["install", "claude", "--dry-run"])

    assert result.exit_code == 0, result.output
    live = runner.invoke(app, ["install", "claude", "--yes"])
    assert live.exit_code == 0, live.output
    assert live.exception is None
    assert _squeeze("WARN could not move settings") in _flat(live)


# F090: the install wording says what install does.


def test_the_prompt_does_not_promise_existing_hooks_when_there_are_none(
    monkeypatch, tmp_path: Path
) -> None:
    _claude_settings(_home(monkeypatch, tmp_path), body=b'{"model": "opus"}')

    result = runner.invoke(app, ["install", "claude"], input="n\n")

    assert "after existing" not in result.output
    assert "Add shim's Claude Code hook?" in result.output


def test_the_prompt_keeps_the_promise_when_hooks_exist(
    monkeypatch, tmp_path: Path
) -> None:
    body = json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/bin/true"}]}]}}
    )
    _claude_settings(_home(monkeypatch, tmp_path), body=body.encode())

    result = runner.invoke(app, ["install", "claude"], input="n\n")

    assert "Append shim after existing Claude Code hooks?" in result.output


def test_the_dry_run_does_not_append_to_a_file_without_hooks(
    monkeypatch, tmp_path: Path
) -> None:
    _claude_settings(_home(monkeypatch, tmp_path), body=b'{"model": "opus"}')

    result = runner.invoke(app, ["install", "claude", "--dry-run"])

    assert _squeeze("Would update Claude Code hooks") in _flat(result)
    assert "append" not in _flat(result)


def test_the_summary_says_the_file_is_rewritten_and_install_does_that(
    monkeypatch, tmp_path: Path
) -> None:
    target = _claude_settings(
        _home(monkeypatch, tmp_path), body=b'{\n    "env": {"RATIO": 1.50}\n}\n'
    )

    preview = runner.invoke(app, ["install", "claude", "--dry-run", "--json"])
    summary = json.loads(preview.stdout)["summary"]
    assert "Nothing else in the file changes" not in summary
    assert "2-space indentation" in summary

    assert runner.invoke(app, ["install", "claude", "--yes"]).exit_code == 0
    written = target.read_text(encoding="utf-8")
    assert '\n  "env": {' in written
    assert json.loads(written)["env"] == {"RATIO": 1.5}


# F094: a [mode] default that weakens the shipped inbound/outbound defaults is shown.


def _shim_settings(body: str) -> Path:
    target = config_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    target.chmod(0o600)
    return target


def test_default_warn_that_turns_masking_off_is_warned_about(
    monkeypatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    _shim_settings('[mode]\nBash = "enforce"\ndefault = "warn"\n')

    check = diagnostics._mode_defaults()

    assert check is not None
    assert check.status == "WARN"
    assert check.code == "MODE_DEFAULT_WEAKENS"
    assert "inbound" in check.detail and "outbound" in check.detail
    assert "inbound = " in check.fix


@pytest.mark.parametrize(
    "body",
    [
        "",
        '[mode]\ndefault = "enforce"\n',
        '[mode]\ndefault = "warn"\ninbound = "enforce"\noutbound = "enforce"\n',
    ],
)
def test_nothing_is_warned_when_masking_stays_on(
    monkeypatch, tmp_path: Path, body: str
) -> None:
    _home(monkeypatch, tmp_path)
    _shim_settings(body)

    assert diagnostics._mode_defaults() is None


# F099: patterns that cannot run are not reported as working.


CUSTOM = (
    'enabled_entities = ["SECRET"]\n\n'
    '[[custom]]\nname = "CODENAME"\npattern = "\\\\bATLAS-[0-9]{4}\\\\b"\n'
)


def test_custom_patterns_with_custom_off_are_a_warning(
    monkeypatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    _shim_settings(CUSTOM)

    check = diagnostics._custom_patterns()

    assert check is not None
    assert check.status == "WARN"
    assert check.code == "CUSTOM_PATTERNS_INACTIVE"
    assert "CODENAME" in check.detail
    assert "--enable CUSTOM" in check.fix


def test_custom_patterns_with_custom_on_still_pass(monkeypatch, tmp_path: Path) -> None:
    _home(monkeypatch, tmp_path)
    _shim_settings(CUSTOM.replace('["SECRET"]', '["SECRET", "CUSTOM"]'))

    check = diagnostics._custom_patterns()

    assert check is not None
    assert check.status == "PASS"


def test_config_shows_that_custom_patterns_are_off(monkeypatch, tmp_path: Path) -> None:
    _home(monkeypatch, tmp_path)
    _shim_settings(CUSTOM)

    result = runner.invoke(app, ["config"])

    assert _squeeze("Custom (off): CODENAME") in _flat(result)


def test_config_shows_custom_patterns_that_are_on(monkeypatch, tmp_path: Path) -> None:
    _home(monkeypatch, tmp_path)
    _shim_settings(CUSTOM.replace('["SECRET"]', '["SECRET", "CUSTOM"]'))

    text = _flat(runner.invoke(app, ["config"]))

    assert _squeeze("Custom: CODENAME") in text
    assert "(off)" not in text


# F119: a 0.2.0 file that is not going to move is not promised a move.


def test_an_unused_legacy_settings_file_gets_an_rm_fix(
    monkeypatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    legacy = legacy_config_path()
    assert legacy is not None
    legacy.parent.mkdir(mode=0o700, parents=True)
    legacy.write_text('enabled_entities = ["EMAIL"]\n', encoding="utf-8")
    current = _shim_settings('enabled_entities = ["EMAIL", "SECRET"]\n')

    check = diagnostics._legacy_state("claude", False)

    assert check.status == "WARN"
    assert check.code == "LEGACY_NAMES_PRESENT"
    assert "unused" in check.detail
    assert str(current) in check.detail
    assert check.fix == f"Run rm {shlex.quote(str(legacy))}."
    assert "move" not in check.detail


def test_a_legacy_settings_file_that_will_move_still_says_so(
    monkeypatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    legacy = legacy_config_path()
    assert legacy is not None
    legacy.parent.mkdir(mode=0o700, parents=True)
    legacy.write_text('enabled_entities = ["EMAIL"]\n', encoding="utf-8")

    check = diagnostics._legacy_state("claude", False)

    assert "move on the next shim config" in check.detail
    assert check.fix == "Run shim config."


# F095: a settings file that is missing is said to be missing.


def test_config_warns_when_there_is_no_settings_file(
    monkeypatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)

    text = _flat(runner.invoke(app, ["config"]))

    assert (
        _squeeze(f"WARN no settings file at {config_path()}; shipped defaults apply")
        in text
    )


def test_doctor_warns_when_there_is_no_settings_file(
    monkeypatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)

    check = diagnostics._entity_settings()

    assert check.status == "WARN"
    assert check.code == "SETTINGS_FILE_ABSENT"
    assert str(config_path()) in check.detail
    assert "shipped defaults apply" in check.detail


def test_doctor_passes_once_there_is_a_settings_file(
    monkeypatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    _shim_settings('enabled_entities = ["EMAIL"]\n')

    assert diagnostics._entity_settings().status == "PASS"


def test_a_shim_config_that_names_a_missing_file_is_refused(
    monkeypatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("SHIM_CONFIG", str(tmp_path / "team" / "shim.toml"))

    check = diagnostics._entity_settings()

    assert check.status == "FAIL"
    assert check.code == "SETTINGS_REFUSED"
    assert "SHIM_CONFIG" in check.detail
    assert str(tmp_path / "team" / "shim.toml") in check.detail


def test_a_dangling_settings_folder_is_refused_not_defaulted(
    monkeypatch, tmp_path: Path
) -> None:
    home = _home(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    (home / ".config").mkdir(mode=0o700)
    (home / ".config" / "shim").symlink_to(tmp_path / "unmounted")

    check = diagnostics._entity_settings()

    assert check.status == "FAIL"
    assert check.code == "SETTINGS_REFUSED"
    assert "broken link" in check.detail


# F098: the diagnostics name the file that was refused, and a fix for that file.


def test_a_refused_legacy_file_is_named_with_its_own_fix(
    monkeypatch, tmp_path: Path
) -> None:
    _home(monkeypatch, tmp_path)
    legacy = legacy_config_path()
    assert legacy is not None
    legacy.parent.mkdir(mode=0o700, parents=True)
    legacy.write_text("ledger = true\n", encoding="utf-8")
    legacy.chmod(0o664)

    check = diagnostics._entity_settings()

    assert check.code == "SETTINGS_REFUSED"
    assert str(legacy) in check.detail
    assert str(config_path()) not in check.detail
    assert shlex.quote(str(legacy)) in check.fix
    assert shlex.quote(str(legacy.parent)) in check.fix


def test_a_relative_shim_config_names_the_variable(monkeypatch, tmp_path: Path) -> None:
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("SHIM_CONFIG", "team/shim.toml")

    check = diagnostics._entity_settings()

    assert check.status == "FAIL"
    assert check.code == "SETTINGS_INVALID"
    assert "SHIM_CONFIG" in check.detail
    assert "absolute" in check.fix
    assert "--reset" not in check.detail


def test_a_relative_xdg_config_home_is_ignored(monkeypatch, tmp_path: Path) -> None:
    home = _home(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")

    assert config_path() == home / ".config" / "shim" / "config.toml"


# The finding texts say what a hook that cannot run really does.


def test_a_settings_hook_that_cannot_import_shim_leaves_the_decision_to_the_client(
    tmp_path: Path,
) -> None:
    """The fact HOOK_RUNNER_FAILED and HOOK_RESOLUTION_FAILED describe, run for real."""
    import subprocess
    import sys

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv], check=True)
    home = tmp_path / "home"
    home.mkdir()
    prompt = json.dumps(
        {"hook_event_name": "UserPromptSubmit", "prompt": "email ops@example.com"}
    )

    ran = subprocess.run(
        [venv / "bin" / "python", "-I", "-B", "-m", "shim_cli.hook", "claude"],
        input=prompt,
        capture_output=True,
        encoding="utf-8",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path)},
        check=False,
    )

    assert ran.returncode == 1 and ran.stdout == ""
    assert "No module named" in ran.stderr


def test_the_finding_texts_say_the_client_decides_and_the_launcher_withholds() -> None:
    from shim_cli.cli.findings import DOCTOR

    runner_en, runner_tr = DOCTOR["HOOK_RUNNER_FAILED"].summary
    resolution_en, resolution_tr = DOCTOR["HOOK_RESOLUTION_FAILED"].summary

    assert "no launcher" in runner_en and "client decides" in runner_en
    assert "başlatıcısı yok" in runner_tr and "istemci karar verir" in runner_tr
    assert "launcher withholds" in resolution_en
    assert "başlatıcısı ise" in resolution_tr and "alıkoyar" in resolution_tr
    assert "so prompts pass uninspected" not in resolution_en
