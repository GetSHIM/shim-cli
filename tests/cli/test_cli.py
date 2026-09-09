from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click import unstyle
from typer.testing import CliRunner

from shim_cli import __version__
from shim_cli.cli import output
from shim_cli.cli.app import app
from shim_cli.cli.output import terminal_text
from shim_cli.config import load_policy
from shim_cli.events.diet import DEFAULT_TRANSFORMS
from shim_cli.guard import DEFAULT_ENTITIES

runner = CliRunner()


def _codex_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _codex(monkeypatch, tmp_path: Path, version: str = "0.149.0") -> None:
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = features ]; then\n'
        "  printf 'hooks stable true\\n'\n"
        "else\n"
        f"  printf 'codex {version}\\n'\n"
        "fi\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def _claude_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _claude(monkeypatch, tmp_path: Path, version: str = "2.1.210") -> None:
    executable = tmp_path / "claude"
    executable.write_text(f"#!/bin/sh\nprintf '{version} (Claude Code)\\n'\n")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def _copilot_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _copilot(monkeypatch, tmp_path: Path, version: str = "1.0.80") -> None:
    executable = tmp_path / "copilot"
    executable.write_text(f"#!/bin/sh\nprintf 'GitHub Copilot CLI {version}.\\n'\n")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def _guard_config(monkeypatch, tmp_path: Path) -> Path:
    target = tmp_path / "settings" / "config.toml"
    monkeypatch.setenv("SHIM_GUARD_CONFIG", str(target))
    return target


def test_help_does_not_load_detector() -> None:
    script = (
        "import sys; from typer.testing import CliRunner; "
        "from shim_cli.cli.app import app; "
        "result = CliRunner().invoke(app, ['--help']); "
        "raise SystemExit(0 if result.exit_code == 0 and 'presidio_analyzer' not in sys.modules else 1)"
    )
    result = subprocess.run([sys.executable, "-I", "-c", script], check=False)

    assert result.returncode == 0


def test_help_remains_readable_at_narrow_terminal_width() -> None:
    result = runner.invoke(
        app,
        ["--help"],
        env={
            "COLUMNS": "20",
            "CI": None,
            "FORCE_COLOR": None,
            "GITHUB_ACTIONS": None,
        },
        color=False,
    )
    rendered = unstyle(result.output)

    assert result.exit_code == 0
    assert "Usage:" in rendered
    assert "Commands" in rendered
    assert max(map(len, rendered.splitlines())) <= 20


COMMANDS = (
    "help",
    "update",
    "demo",
    "scan",
    "redact",
    "config",
    "install",
    "status",
    "doctor",
    "revert",
    "report",
    "watch",
    "ledger",
)


def test_no_rendered_help_says_guard() -> None:
    rendered = {
        name: unstyle(runner.invoke(app, [*name.split(), "--help"], color=False).output)
        for name in ("--help", *COMMANDS)
    }

    assert sorted(name for name, text in rendered.items() if "Guard" in text) == []


def test_help_command_lists_a_description_for_every_command() -> None:
    result = runner.invoke(app, ["help"], color=False)
    rendered = unstyle(result.output)
    descriptions = (
        "Show help.",
        "Update shim.",
        "Run a synthetic detector check.",
        "Scan UTF-8 stdin.",
        "Redact UTF-8 stdin.",
        "Show or change detection settings.",
        "Preview or install a client hook.",
        "Show hook status.",
        "Check client and hook health.",
        "Remove shim's client hook.",
    )

    assert result.exit_code == 0
    assert "Usage: shim [OPTIONS]" in rendered
    assert "[ARGS]..." in rendered
    assert "--version" in rendered
    assert all(description in rendered for description in descriptions)


def test_version_option_reports_package_version() -> None:
    result = runner.invoke(app, ["--version"], color=False)

    assert result.exit_code == 0
    assert result.output == f"shim {__version__}\n"


def test_update_uses_the_original_package_manager(monkeypatch) -> None:
    calls = []
    packages = []

    def run(command, *, check):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("shim_cli.cli.app.subprocess.run", run)
    for installer in ("uv", "pip"):
        distribution = SimpleNamespace(
            read_text=lambda _, installer=installer: installer
        )

        def get_distribution(package, distribution=distribution):
            packages.append(package)
            return distribution

        monkeypatch.setattr("shim_cli.cli.app.metadata.distribution", get_distribution)
        assert runner.invoke(app, ["update"]).exit_code == 0

    assert packages == ["shim", "shim"]
    assert calls == [
        ("uv", "tool", "upgrade", "shim"),
        ("pipx", "upgrade", "shim"),
    ]


def test_client_arguments_list_and_enforce_available_value() -> None:
    for command in ("demo", "install", "status", "doctor", "revert"):
        help_result = runner.invoke(app, [command, "--help"], color=False)
        invalid_result = runner.invoke(app, [command, "other"], color=False)

        assert help_result.exit_code == 0
        assert "claude" in help_result.output
        assert "codex" in help_result.output
        assert "copilot" in help_result.output
        assert invalid_result.exit_code == 2
        assert "'claude'" in invalid_result.output
        assert "'codex'" in invalid_result.output
        assert "'copilot'" in invalid_result.output


def test_privacy_stdin_json_and_no_color(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    safe = runner.invoke(app, ["scan"], input="plain synthetic text")
    finding = runner.invoke(app, ["scan", "--json"], input="email alice@example.com")
    redacted = runner.invoke(app, ["redact"], input="email alice@example.com")
    redacted_json = runner.invoke(
        app, ["redact", "--json"], input="Contact alice@example.invalid"
    )
    demo = runner.invoke(app, ["demo", "codex", "--json"])
    invalid = runner.invoke(app, ["scan"], input=b"\xff")

    assert safe.exit_code == 0
    assert "\x1b" not in safe.output
    assert finding.exit_code == 1
    assert "alice@example.com" not in finding.output
    assert json.loads(finding.output)["schema_version"] == 1
    assert redacted.exit_code == 0
    assert redacted.output == "email <EMAIL_1>\n"
    assert "alice@example.invalid" not in redacted_json.output
    assert "redacted_text" not in json.loads(redacted_json.output)
    assert "redacted_text" not in json.loads(demo.output)
    assert invalid.exit_code == 1
    assert "Unable to process stdin" in invalid.output


def test_config_selects_entities_for_privacy_commands(
    monkeypatch, tmp_path: Path
) -> None:
    target = _guard_config(monkeypatch, tmp_path)

    initial = runner.invoke(app, ["config"], color=False)
    path_scan = runner.invoke(
        app,
        ["scan", "--json"],
        input="Read /Users/alice/.ssh/id_rsa, then continue",
    )
    saved = runner.invoke(
        app,
        ["config", "--only", "EMAIL", "--only", "SECRET", "--yes"],
        color=False,
    )
    scan = runner.invoke(
        app,
        ["scan", "--json"],
        input="alice@example.com +90 532 123 45 67",
    )
    current = runner.invoke(app, ["config", "--json"])
    narrow = runner.invoke(app, ["config"], env={"COLUMNS": "20"}, color=False)
    adjusted = runner.invoke(
        app,
        ["config", "--enable", "phone", "--disable", "secret", "--yes"],
    )
    final = runner.invoke(app, ["config", "--json"])

    assert path_scan.exit_code == 0
    assert json.loads(path_scan.output)["status"] == "safe"
    assert initial.exit_code == saved.exit_code == current.exit_code == 0
    assert adjusted.exit_code == final.exit_code == 0
    assert "Current detection: 12/12 enabled" in initial.output
    assert "ON" in saved.output and "OFF" in saved.output
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(scan.output)["counts"] == {"EMAIL": 1}
    payload = json.loads(current.output)
    assert payload["enabled_entities"] == ["EMAIL", "SECRET"]
    assert "PHONE" in payload["disabled_entities"]
    assert "OFF TR_NATIONAL_ID" in narrow.output
    assert max(map(len, narrow.output.splitlines())) <= 20
    assert json.loads(final.output)["enabled_entities"] == ["EMAIL", "PHONE"]


def test_config_recovers_invalid_settings_and_rejects_conflicts(
    monkeypatch, tmp_path: Path
) -> None:
    target = _guard_config(monkeypatch, tmp_path)
    target.parent.mkdir()
    target.write_bytes(b"not toml")

    blocked = runner.invoke(app, ["scan"], input="plain text")
    conflict = runner.invoke(
        app, ["config", "--enable", "EMAIL", "--disable", "EMAIL", "--yes"]
    )
    reset = runner.invoke(app, ["config", "--reset", "--yes"])

    assert blocked.exit_code == 1
    assert "Unable to process stdin" in blocked.output
    assert conflict.exit_code == 2
    assert "cannot be enabled and disabled" in conflict.output
    assert reset.exit_code == 0
    assert "EMAIL" in target.read_text()


def test_empty_no_color_value_disables_ansi(monkeypatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(output.sys, "stdout", stream)
    monkeypatch.setenv("NO_COLOR", "")

    output.emit("PASS", "Readable without color.")

    assert "\x1b" not in stream.getvalue()


def test_redaction_escapes_terminal_controls_only_for_a_tty(monkeypatch) -> None:
    monkeypatch.setattr(output.sys.stdout, "isatty", lambda: True)
    assert terminal_text("safe\x1b[31m", output.sys.stdout) == r"safe\x1b[31m"

    monkeypatch.setattr(output.sys.stdout, "isatty", lambda: False)
    assert terminal_text("safe\x1b[31m", output.sys.stdout) == "safe\x1b[31m"


def test_install_status_and_revert(monkeypatch, tmp_path: Path) -> None:
    home = _codex_home(monkeypatch, tmp_path)
    target = home / ".codex" / "hooks.json"

    preview = runner.invoke(app, ["install", "codex", "--dry-run"])
    assert preview.exit_code == 0
    assert not target.exists()

    installed = runner.invoke(app, ["install", "codex", "--yes"])
    current = runner.invoke(app, ["status", "codex", "--json"])
    reverted = runner.invoke(app, ["revert", "codex", "--yes"])

    assert installed.exit_code == 0
    assert json.loads(current.output)["state"] == "installed"
    assert reverted.exit_code == 0
    assert target.read_bytes() == b"{}\n"


def test_claude_install_status_doctor_and_revert(monkeypatch, tmp_path: Path) -> None:
    from shim_cli.clients.claude.settings import hook_group

    home = _claude_home(monkeypatch, tmp_path)
    _claude(monkeypatch, tmp_path)
    target = home / ".claude" / "settings.json"
    original = {
        "permissions": {"allow": ["Read"]},
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "existing-hook"}]}
            ]
        },
    }
    target.write_text(json.dumps(original))

    preview = runner.invoke(app, ["install", "claude", "--dry-run"])
    installed = runner.invoke(app, ["install", "claude", "--yes"])
    installed_document = json.loads(target.read_bytes())
    current = runner.invoke(app, ["status", "claude", "--json"])
    doctor = runner.invoke(app, ["doctor", "claude", "--json"])
    reverted = runner.invoke(app, ["revert", "claude", "--yes"])

    assert preview.exit_code == installed.exit_code == reverted.exit_code == 0
    assert "existing-hook" not in preview.output
    assert "appended last" in " ".join(installed.output.split())
    assert json.loads(current.output)["state"] == "installed"
    doctor_payload = json.loads(doctor.output)
    assert doctor_payload["client"] == "claude"
    assert doctor_payload["status"] == "warning"
    assert {item["name"] for item in doctor_payload["checks"]} == {
        "claude",
        "hook_configuration",
        "legacy_names",
        "entity_settings",
        "custom_patterns",
        "session_record",
        "runner",
        "hook_resolution",
        "duplicate_hooks",
        "coverage",
        "hook_activation",
    }
    assert installed_document["permissions"] == original["permissions"]
    assert installed_document["hooks"]["UserPromptSubmit"][-1] == hook_group()
    assert json.loads(target.read_bytes()) == original


def test_copilot_install_status_doctor_and_revert(monkeypatch, tmp_path: Path) -> None:
    from shim_cli.clients.copilot.settings import hook_document

    home = _copilot_home(monkeypatch, tmp_path)
    _copilot(monkeypatch, tmp_path)
    target = home / ".copilot" / "hooks" / "shim.json"

    missing = runner.invoke(app, ["status", "copilot", "--json"])
    preview = runner.invoke(app, ["install", "copilot", "--dry-run"])
    assert not (home / ".copilot").exists()
    installed = runner.invoke(app, ["install", "copilot", "--yes"])
    current = runner.invoke(app, ["status", "copilot", "--json"])
    doctor = runner.invoke(app, ["doctor", "copilot", "--json"])
    reverted = runner.invoke(app, ["revert", "copilot", "--yes"])

    assert preview.exit_code == installed.exit_code == reverted.exit_code == 0
    assert missing.exit_code == 1
    assert json.loads(missing.output)["state"] == "not_installed"
    assert json.loads(current.output)["state"] == "installed"
    assert json.loads(doctor.output)["status"] == "warning"
    # The file is shim's own, so revert deletes it rather than leaving an
    # empty `{"version": 1, "hooks": {}}` shell behind.
    assert not target.exists()
    assert json.loads(preview.output[preview.output.index("{") :]) == hook_document()


def test_confirmation_and_doctor(monkeypatch, tmp_path: Path) -> None:
    _codex_home(monkeypatch, tmp_path)
    _codex(monkeypatch, tmp_path)

    cancelled = runner.invoke(app, ["install", "codex"], input="n\n")
    doctor = runner.invoke(app, ["doctor", "codex", "--json"])

    assert cancelled.exit_code == 1
    assert "cancelled" in cancelled.output.lower()
    payload = json.loads(doctor.output)
    assert payload["schema_version"] == 1
    assert {item["name"] for item in payload["checks"]} == {
        "codex",
        "hooks_feature",
        "hook_configuration",
        "legacy_names",
        "entity_settings",
        "custom_patterns",
        "session_record",
        "runner",
        "hook_resolution",
        "duplicate_hooks",
        "coverage",
        "hook_activation",
    }
    assert payload["status"] == "warning"


def test_install_preserves_shared_hooks_and_preview_hides_them(
    monkeypatch, tmp_path: Path
) -> None:
    from shim_cli.clients.codex.settings import hook_group

    home = _codex_home(monkeypatch, tmp_path)
    target = home / ".codex" / "hooks.json"
    existing_group = {"hooks": [{"type": "command", "command": "existing-secret-hook"}]}
    original = {
        "version": 1,
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "session-hook"}]}
            ],
            "UserPromptSubmit": [existing_group],
        },
    }
    target.write_text(json.dumps(original))

    preview = runner.invoke(app, ["install", "codex", "--dry-run"])
    installed = runner.invoke(app, ["install", "codex", "--yes"])
    first_install = target.read_bytes()
    repeated = runner.invoke(app, ["install", "codex", "--yes"])
    reverted = runner.invoke(app, ["revert", "codex", "--yes"])

    assert preview.exit_code == installed.exit_code == repeated.exit_code == 0
    assert "existing-secret-hook" not in preview.output
    assert "will be preserved" in installed.output
    assert "appended last" in installed.output
    installed_document = json.loads(first_install)
    assert installed_document["version"] == 1
    assert (
        installed_document["hooks"]["SessionStart"] == original["hooks"]["SessionStart"]
    )
    assert installed_document["hooks"]["UserPromptSubmit"] == [
        existing_group,
        hook_group(),
    ]
    assert json.loads(target.read_bytes()) == original
    assert reverted.exit_code == 0


def test_repeated_install_does_not_reformat_existing_document(
    monkeypatch, tmp_path: Path
) -> None:
    from shim_cli.clients.codex.settings import hook_group

    home = _codex_home(monkeypatch, tmp_path)
    target = home / ".codex" / "hooks.json"
    content = json.dumps(
        {"hooks": {"UserPromptSubmit": [hook_group()]}}, separators=(",", ":")
    ).encode()
    target.write_bytes(content)

    result = runner.invoke(app, ["install", "codex", "--yes"])

    assert result.exit_code == 0
    assert target.read_bytes() == content


def test_install_leaves_inline_hooks_untouched(monkeypatch, tmp_path: Path) -> None:
    home = _codex_home(monkeypatch, tmp_path)
    config = home / ".codex" / "config.toml"
    inline = (
        "[hooks]\n"
        'UserPromptSubmit = [{ hooks = [{ type = "command", command = "inline" }] }]\n'
    )
    config.write_text(inline)

    result = runner.invoke(app, ["install", "codex", "--yes"])

    assert result.exit_code == 0
    assert "stay untouched" in result.output
    assert config.read_text() == inline
    assert (home / ".codex" / "hooks.json").exists()


def test_install_refuses_hook_document_changed_during_confirmation(
    monkeypatch, tmp_path: Path
) -> None:
    home = _codex_home(monkeypatch, tmp_path)
    target = home / ".codex" / "hooks.json"
    target.write_text('{"hooks":{"UserPromptSubmit":[]}}')
    changed = {
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "concurrent-hook"}]}
            ]
        }
    }

    def change_hooks(*_args, **_kwargs) -> bool:
        target.write_text(json.dumps(changed))
        return True

    monkeypatch.setattr("shim_cli.cli.integrations.typer.confirm", change_hooks)
    result = runner.invoke(app, ["install", "codex"])

    assert result.exit_code == 2
    assert json.loads(target.read_bytes()) == changed


def test_install_refuses_malformed_hook_document(monkeypatch, tmp_path: Path) -> None:
    home = _codex_home(monkeypatch, tmp_path)
    target = home / ".codex" / "hooks.json"
    target.write_bytes(b'{"hooks":')

    result = runner.invoke(app, ["install", "codex", "--yes"])

    assert result.exit_code == 2
    assert target.read_bytes() == b'{"hooks":'


def test_install_refuses_when_detector_warmup_fails(
    monkeypatch, tmp_path: Path
) -> None:
    home = _codex_home(monkeypatch, tmp_path)
    target = home / ".codex" / "hooks.json"

    def fail(_: str) -> None:
        raise RuntimeError

    monkeypatch.setattr("shim_cli.guard.evaluate", fail)
    result = runner.invoke(app, ["install", "codex", "--yes"])

    assert result.exit_code == 2
    assert "detector could not start" in result.output
    assert not target.exists()


def test_doctor_version_states(monkeypatch, tmp_path: Path) -> None:
    """Read the boundaries from the constants; a bump must not edit this test."""
    from shim_cli.clients.codex.settings import (
        MINIMUM_CODEX_VERSION,
        TESTED_CODEX_VERSION,
    )

    _codex_home(monkeypatch, tmp_path)

    def status(version: str | None) -> str:
        if version is None:
            monkeypatch.setenv("PATH", "")
        else:
            _codex(monkeypatch, tmp_path, version)
        result = runner.invoke(app, ["doctor", "codex", "--json"])
        return json.loads(result.output)["checks"][0]["status"]

    major, minor, patch = (int(part) for part in MINIMUM_CODEX_VERSION.split("."))
    below = f"{major}.{minor}.{patch - 1}"
    beyond = f"{major}.{minor + 100}.0"

    assert status(None) == "FAIL"
    assert status(below) == "FAIL"
    assert status(MINIMUM_CODEX_VERSION) == "PASS"
    assert status(TESTED_CODEX_VERSION) == "PASS"
    assert status(beyond) == "WARN"


def test_config_preserves_the_sections_it_does_not_change(monkeypatch, tmp_path: Path):
    target = _guard_config(monkeypatch, tmp_path)
    target.parent.mkdir()
    target.write_text(
        'enabled_entities = ["EMAIL"]\n\n'
        "[entities]\n"
        'Read = ["SECRET"]\n\n'
        "[mode]\n"
        'user-prompt = "enforce"\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["config", "--enable", "SECRET", "--yes"])

    assert result.exit_code == 0
    saved = target.read_text()
    assert 'user-prompt = "enforce"' in saved
    assert "Read = [" in saved
    assert '"SECRET"' in saved

    policy = load_policy(target)
    assert policy.mode_for("user-prompt") == "enforce"
    assert policy.entities_for("Read") == ("SECRET",)


def test_ledger_is_off_until_it_is_turned_on(monkeypatch, tmp_path: Path) -> None:
    target = _guard_config(monkeypatch, tmp_path)

    assert runner.invoke(app, ["config", "--reset", "--yes"]).exit_code == 0
    assert load_policy(target).ledger is False

    assert runner.invoke(app, ["config", "--ledger", "--yes"]).exit_code == 0
    assert load_policy(target).ledger is True
    assert "ledger = true" in target.read_text()

    assert runner.invoke(app, ["config", "--no-ledger", "--yes"]).exit_code == 0
    assert load_policy(target).ledger is False
    assert "ledger" not in target.read_text()


def test_reset_restores_every_section_not_only_the_entity_list(
    monkeypatch, tmp_path: Path
) -> None:
    target = _guard_config(monkeypatch, tmp_path)
    target.parent.mkdir()
    target.write_text(
        'enabled_entities = ["EMAIL"]\nledger = true\n\n[mode]\nuser-prompt = "enforce"\n',
        encoding="utf-8",
    )

    assert runner.invoke(app, ["config", "--reset", "--yes"]).exit_code == 0

    policy = load_policy(target)
    assert policy.modes == {}
    assert policy.ledger is False
    assert policy.entities == DEFAULT_ENTITIES


def test_report_says_so_when_there_is_no_session(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", str(tmp_path / "spools"))

    result = runner.invoke(app, ["report"])

    assert result.exit_code == 1
    assert "No session on record" in result.output


def test_report_renders_the_most_recent_session(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", str(tmp_path / "spools"))
    from shim_cli.session import spool

    spool.append(
        "a-session",
        {
            "action": "mask",
            "tool_name": "Read",
            "target": "/work/.env",
            "entities": {"SECRET": 2},
            "latency_ms": 8,
        },
    )

    result = runner.invoke(app, ["report"])
    document = json.loads(runner.invoke(app, ["report", "--json"]).output)

    assert result.exit_code == 0
    assert "2 SECRET" in result.output
    assert document["actions"]["mask"]["entities"] == {"SECRET": 2}


def test_ledger_purge_deletes_only_what_is_retained(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SHIM_GUARD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SHIM_GUARD_SESSION_DIR", str(tmp_path / "spools"))
    from shim_cli.session import ledger, spool

    ledger.append({"action": "mask", "entities": {"SECRET": 1}})
    spool.append("live", {"action": "mask", "entities": {"SECRET": 1}})

    empty = runner.invoke(app, ["ledger", "purge", "--yes"])

    assert empty.exit_code == 0
    assert ledger.files() == []
    assert spool.entries("live"), "purging the ledger must not touch the session"

    again = runner.invoke(app, ["ledger", "purge", "--yes"])
    assert again.exit_code == 0
    assert "nothing retained" in again.output


def test_diet_ships_on_and_can_be_turned_off(monkeypatch, tmp_path: Path) -> None:
    from shim_cli.events.diet import DEFAULT_TRANSFORMS

    target = _guard_config(monkeypatch, tmp_path)

    assert runner.invoke(app, ["config", "--reset", "--yes"]).exit_code == 0
    assert load_policy(target).diet == DEFAULT_TRANSFORMS

    assert runner.invoke(app, ["config", "--no-diet", "--yes"]).exit_code == 0
    assert load_policy(target).diet == ()
    assert "diet = false" in target.read_text()

    assert runner.invoke(app, ["config", "--diet", "--yes"]).exit_code == 0
    assert load_policy(target).diet == DEFAULT_TRANSFORMS


def test_a_single_transform_can_be_named_in_the_config_file(
    monkeypatch, tmp_path: Path
) -> None:
    target = _guard_config(monkeypatch, tmp_path)
    target.parent.mkdir()
    target.write_text(
        'enabled_entities = ["EMAIL"]\ndiet = ["whitespace"]\n', encoding="utf-8"
    )

    assert load_policy(target).diet == ("whitespace",)

    assert runner.invoke(app, ["config", "--enable", "SECRET", "--yes"]).exit_code == 0
    assert load_policy(target).diet == ("whitespace",)


def test_report_reads_the_ledger_once_the_session_has_ended(tmp_path: Path) -> None:
    from shim_cli.session import ledger

    ledger.append(
        {
            "session_id": "ended",
            "ts": "2026-08-30T09:00:00Z",
            "action": "mask",
            "tool_name": "Read",
            "target": "/work/.env",
            "entities": {"SECRET": 1},
            "latency_ms": 4,
        }
    )

    result = runner.invoke(app, ["report"])
    document = json.loads(runner.invoke(app, ["report", "--json"]).output)

    assert result.exit_code == 0
    assert "1 SECRET" in result.output
    assert "retained ledger" in result.output
    assert document["source"] == "ledger"


def test_report_prefers_the_live_session_over_the_ledger() -> None:
    from shim_cli.session import ledger, spool

    ledger.append(
        {
            "session_id": "ended",
            "ts": "2026-08-30T09:00:00Z",
            "action": "mask",
            "entities": {"IBAN": 9},
        }
    )
    spool.append("live", {"action": "mask", "entities": {"SECRET": 1}})

    document = json.loads(runner.invoke(app, ["report", "--json"]).output)

    assert document["source"] == "session"
    assert document["actions"]["mask"]["entities"] == {"SECRET": 1}


def test_report_shows_one_session_not_the_whole_month() -> None:
    from shim_cli.session import ledger

    for session, when, entity in (
        ("older", "2026-08-29T10:00:00Z", "IBAN"),
        ("newest", "2026-08-30T11:00:00Z", "SECRET"),
        ("newest", "2026-08-30T11:00:01Z", "EMAIL"),
    ):
        ledger.append(
            {
                "session_id": session,
                "ts": when,
                "action": "mask",
                "entities": {entity: 1},
            }
        )

    document = json.loads(runner.invoke(app, ["report", "--json"]).output)

    assert document["actions"]["mask"]["entities"] == {"EMAIL": 1, "SECRET": 1}


def test_report_says_none_when_neither_source_has_anything() -> None:
    document = json.loads(runner.invoke(app, ["report", "--json"]).output)

    assert document["source"] == "none"


def test_config_shows_whether_records_are_being_kept() -> None:
    off = json.loads(runner.invoke(app, ["config", "--json"]).output)

    assert off["ledger"] is False
    assert off["diet"] == list(DEFAULT_TRANSFORMS)

    runner.invoke(app, ["config", "--ledger", "--no-diet", "--yes", "--json"])
    on = json.loads(runner.invoke(app, ["config", "--json"]).output)

    assert on["ledger"] is True
    assert on["diet"] == []

    assert "Ledger: on" in runner.invoke(app, ["config"]).output


@pytest.mark.parametrize(
    ("client", "relative"),
    (
        ("claude", ".claude/settings.json"),
        ("codex", ".codex/hooks.json"),
        ("copilot", ".copilot/hooks/shim.json"),
    ),
)
def test_install_creates_a_config_directory_that_does_not_exist_yet(
    monkeypatch, tmp_path: Path, client: str, relative: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for variable in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    target = home / relative
    assert not target.parent.exists()

    installed = runner.invoke(app, ["install", client, "--yes"])

    assert installed.exit_code == 0, installed.output
    assert target.exists()
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert (
        json.loads(runner.invoke(app, ["status", client, "--json"]).output)["state"]
        == "installed"
    )


def test_config_refuses_change_during_confirmation(monkeypatch, tmp_path):
    target = tmp_path / "config.toml"
    target.write_text('enabled_entities = ["EMAIL"]\n')
    monkeypatch.setenv("SHIM_GUARD_CONFIG", str(target))
    concurrent = b'enabled_entities = ["SECRET"]\n[mode]\nuser-prompt = "enforce"\n'

    def confirm(*args, **kwargs):
        target.write_bytes(concurrent)
        return True

    monkeypatch.setattr("typer.confirm", confirm)
    result = CliRunner().invoke(app, ["config", "--enable", "PHONE"])
    assert result.exit_code == 2
    assert target.read_bytes() == concurrent


def test_config_refuses_file_created_during_confirmation(monkeypatch, tmp_path):
    target = tmp_path / "new-parent" / "config.toml"
    monkeypatch.setenv("SHIM_GUARD_CONFIG", str(target))
    concurrent = b"ledger = true\n"

    def confirm(*args, **kwargs):
        target.write_bytes(concurrent)
        return True

    monkeypatch.setattr("typer.confirm", confirm)
    result = CliRunner().invoke(app, ["config", "--enable", "PHONE"])
    assert result.exit_code == 2
    assert target.read_bytes() == concurrent


def test_the_coverage_table_says_what_stop_sees_and_that_it_changes_nothing(
    monkeypatch, tmp_path: Path
) -> None:
    from shim_cli.cli.diagnostics import _coverage_rows

    rows = {row["event"]: row for row in _coverage_rows("claude")}

    assert "last_assistant_message" in rows["Stop"]["sees"]
    assert rows["Stop"]["can_mask"] is False
    assert rows["Stop"]["can_report"] is True


def test_custom_patterns_are_written_read_and_removed(monkeypatch, tmp_path) -> None:
    target = _guard_config(monkeypatch, tmp_path)

    added = runner.invoke(
        app,
        ["config", "--custom", r"CODENAME=\bATLAS-[0-9]{4}\b", "--yes"],
        color=False,
    )
    scanned = runner.invoke(app, ["scan", "--json"], input="ship ATLAS-0042")
    removed = runner.invoke(
        app, ["config", "--remove-custom", "CODENAME", "--yes"], color=False
    )
    after = runner.invoke(app, ["scan", "--json"], input="ship ATLAS-0042")

    assert added.exit_code == 0
    assert json.loads(scanned.output)["counts"] == {"CUSTOM": 1}
    assert removed.exit_code == 0
    assert json.loads(after.output)["counts"] == {}
    assert "custom" not in target.read_text(encoding="utf-8")


def test_a_literal_is_written_and_matched_whole(monkeypatch, tmp_path) -> None:
    _guard_config(monkeypatch, tmp_path)

    runner.invoke(app, ["config", "--custom-literal", "CODENAME=atlas", "--yes"])
    matched = runner.invoke(app, ["scan", "--json"], input="atlas ships")
    missed = runner.invoke(app, ["scan", "--json"], input="atlases ship")

    assert json.loads(matched.output)["counts"] == {"CUSTOM": 1}
    assert json.loads(missed.output)["counts"] == {}


@pytest.mark.parametrize(
    "value", (r"BAD=(a+)+$", r"BACKREF=(a)\1", "NOEQUALS", "lower=x")
)
def test_a_pattern_that_cannot_be_trusted_is_refused(
    monkeypatch, tmp_path, value: str
) -> None:
    target = _guard_config(monkeypatch, tmp_path)
    before = target.read_bytes() if target.exists() else None

    result = runner.invoke(app, ["config", "--custom", value, "--yes"], color=False)

    assert result.exit_code == 2
    assert (target.read_bytes() if target.exists() else None) == before


def test_a_backtracking_pattern_names_itself(monkeypatch, tmp_path) -> None:
    _guard_config(monkeypatch, tmp_path)

    result = runner.invoke(app, ["config", "--custom", "BAD=(a+)+$", "--yes"])

    assert "BAD backtracks on repeated input" in unstyle(result.output)


def test_the_same_name_twice_replaces_rather_than_duplicates(
    monkeypatch, tmp_path
) -> None:
    target = _guard_config(monkeypatch, tmp_path)

    runner.invoke(app, ["config", "--custom", "CODENAME=alpha", "--yes"])
    runner.invoke(app, ["config", "--custom", "CODENAME=beta", "--yes"])

    body = target.read_text(encoding="utf-8")
    assert body.count("CODENAME") == 1
    assert "beta" in body and "alpha" not in body


def test_doctor_reports_a_pattern_that_is_already_in_the_file(
    monkeypatch, tmp_path
) -> None:
    target = _guard_config(monkeypatch, tmp_path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text(
        'enabled_entities = ["CUSTOM"]\n[[custom]]\nname = "BAD"\npattern = "(a+)+$"\n',
        encoding="utf-8",
    )
    target.chmod(0o600)

    result = runner.invoke(app, ["doctor", "claude", "--json"])

    checks = {item["name"]: item for item in json.loads(result.output)["checks"]}
    assert checks["custom_patterns"]["status"] == "FAIL"


def test_reveal_is_written_honoured_and_removed(monkeypatch, tmp_path) -> None:
    target = _guard_config(monkeypatch, tmp_path)
    iban = "TR330006100519786457841326"

    runner.invoke(app, ["config", "--reveal", "IBAN=4", "--yes"])
    revealed = runner.invoke(app, ["redact"], input=f"pay {iban}")
    runner.invoke(app, ["config", "--no-reveal", "IBAN", "--yes"])
    plain = runner.invoke(app, ["redact"], input=f"pay {iban}")

    assert "<IBAN_1:1326>" in revealed.output
    assert "<IBAN_1>" in plain.output and ":1326" not in plain.output
    assert "reveal" not in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("value", ("SECRET=4", "IBAN=9", "IBAN=x", "EMAIL=2"))
def test_a_reveal_that_is_not_allowed_is_refused(
    monkeypatch, tmp_path, value: str
) -> None:
    target = _guard_config(monkeypatch, tmp_path)
    before = target.read_bytes() if target.exists() else None

    result = runner.invoke(app, ["config", "--reveal", value, "--yes"], color=False)

    assert result.exit_code == 2
    assert (target.read_bytes() if target.exists() else None) == before


def test_the_doctor_fixture_ignores_the_user_s_own_settings(
    monkeypatch, tmp_path
) -> None:
    """SHIM_CONFIG outranks the 0.2.0 name; the self-test must still be isolated."""
    from shim_cli.cli.diagnostics import _runner_check

    target = tmp_path / "settings" / "config.toml"
    target.parent.mkdir(mode=0o700, parents=True)
    target.write_text('[mode]\nuser-prompt = "enforce"\n', encoding="utf-8")
    target.chmod(0o600)
    monkeypatch.setenv("SHIM_CONFIG", str(target))
    monkeypatch.delenv("SHIM_GUARD_CONFIG", raising=False)

    assert _runner_check("claude").status == "PASS"


def test_a_venv_install_is_not_reported_as_no_hook(monkeypatch, tmp_path) -> None:
    """`shim install` writes an absolute interpreter path, so PATH is irrelevant."""
    from shim_cli.cli import diagnostics
    from shim_cli.cli.resolution import Resolution

    monkeypatch.setattr(
        diagnostics,
        "resolve",
        lambda: Resolution(
            "none",
            "No hook is runnable; prompts are passing through uninspected.",
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        diagnostics, "_installed_hook_runs_this_package", lambda _client: True
    )

    check = diagnostics._resolution_check("codex")

    assert check.status == "PASS"
    assert "nothing is needed on PATH" in check.detail


def test_no_hook_anywhere_is_still_a_failure(monkeypatch, tmp_path) -> None:
    from shim_cli.cli import diagnostics
    from shim_cli.cli.resolution import Resolution

    monkeypatch.setattr(
        diagnostics,
        "resolve",
        lambda: Resolution(
            "none",
            "No hook is runnable; prompts are passing through uninspected.",
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        diagnostics, "_installed_hook_runs_this_package", lambda _client: False
    )

    assert diagnostics._resolution_check("codex").status == "FAIL"


def _legacy_claude_settings(home: Path, *, foreign: bool = False) -> Path:
    from shim_cli.clients.claude.settings import legacy_hook_groups
    from shim_cli.clients.hook_settings import add_groups

    document = json.loads(add_groups(None, legacy_hook_groups()))
    if foreign:
        document["hooks"]["UserPromptSubmit"].append(
            {"hooks": [{"type": "command", "command": "existing-hook"}]}
        )
    target = home / ".claude" / "settings.json"
    target.write_text(json.dumps(document, indent=2))
    return target


def test_doctor_on_a_020_fragment_does_not_also_say_it_is_not_installed(
    monkeypatch, tmp_path: Path
) -> None:
    """The review found both lines two apart. The first one is false: the hook
    is installed, in the shape 0.2.0 wrote."""
    home = _claude_home(monkeypatch, tmp_path)
    _claude(monkeypatch, tmp_path)
    _legacy_claude_settings(home)

    result = runner.invoke(app, ["doctor", "claude"])
    text = " ".join(unstyle(result.output).split())

    assert (
        "hook installed in the 0.2.0 shape; run shim install claude to move it" in text
    )
    assert "hook group is not installed" not in text

    payload = json.loads(runner.invoke(app, ["doctor", "claude", "--json"]).output)
    names = {item["name"] for item in payload["checks"]}
    assert "legacy_names" in names
    assert "hook_configuration" not in names


def test_install_over_a_020_fragment_says_it_replaced_the_line(
    monkeypatch, tmp_path: Path
) -> None:
    home = _claude_home(monkeypatch, tmp_path)
    _claude(monkeypatch, tmp_path)
    _legacy_claude_settings(home)

    result = runner.invoke(app, ["install", "claude", "--yes"])
    text = " ".join(unstyle(result.output).split())

    assert result.exit_code == 0
    assert "Replaced the 0.2.0 hook line with the current one." in text
    # Nothing was preserved and nothing was appended after anything.
    assert "will be preserved" not in text
    assert "Appended shim after existing" not in text


def test_install_over_a_020_fragment_beside_a_foreign_hook_says_both(
    monkeypatch, tmp_path: Path
) -> None:
    home = _claude_home(monkeypatch, tmp_path)
    _claude(monkeypatch, tmp_path)
    target = _legacy_claude_settings(home, foreign=True)

    result = runner.invoke(app, ["install", "claude", "--yes"])
    text = " ".join(unstyle(result.output).split())

    assert "Replaced the 0.2.0 hook line with the current one." in text
    assert "will be preserved" in text
    assert "existing-hook" in target.read_text(encoding="utf-8")


def _broken_config(tmp_path: Path) -> Path:
    target = tmp_path / "shim-roots" / "config" / "shim" / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        'enabled_entities = ["EMAIL"]\nledger = true\nbroken line here\n',
        encoding="utf-8",
    )
    return target


@pytest.mark.parametrize(
    "command",
    [["doctor", "claude"], ["config", "--enable", "IBAN", "--yes"]],
)
def test_a_broken_settings_file_names_the_file_the_line_and_the_way_out(
    command: list[str], monkeypatch, tmp_path: Path
) -> None:
    """`Entity settings are unsafe or invalid` named none of the three, and
    every prompt was withheld until the user guessed which was wrong."""
    _claude_home(monkeypatch, tmp_path)
    _claude(monkeypatch, tmp_path)
    target = _broken_config(tmp_path)

    result = runner.invoke(app, command)
    # Rich wraps long paths, so compare with the whitespace removed.
    text = " ".join(unstyle(result.output).split())
    dense = text.replace(" ", "")

    assert str(target).replace(" ", "") in dense
    assert "line3" in dense
    assert "shimconfig--reset" in dense


def test_the_hook_says_nothing_about_the_contents_of_a_broken_settings_file(
    monkeypatch, tmp_path: Path
) -> None:
    """Doctor may quote the parser. The hook may not: its output reaches the
    model, and a settings file is the user's, not the model's."""
    from shim_cli.hook import main

    target = _broken_config(tmp_path)
    monkeypatch.setattr(
        "sys.stdin",
        SimpleNamespace(
            buffer=io.BytesIO(
                b'{"hook_event_name":"UserPromptSubmit","prompt":"hello"}'
            )
        ),
    )
    written: list[bytes] = []
    monkeypatch.setattr(
        "sys.stdout",
        SimpleNamespace(
            buffer=SimpleNamespace(write=written.append, flush=lambda: None)
        ),
    )

    monkeypatch.setattr("sys.argv", ["shim-hook", "claude"])
    with contextlib.suppress(SystemExit):
        main()

    output_text = b"".join(written).decode("utf-8")
    assert "broken line here" not in output_text
    assert str(target) not in output_text
    assert "shim doctor claude" in output_text


def test_the_ledger_can_be_read_back_not_only_deleted(monkeypatch, tmp_path) -> None:
    """`shim ledger purge` was the only ledger command, so the opt-in record
    was write-only: enabling it to prove something meant reading JSONL by hand.
    """
    from shim_cli.session import ledger

    root = tmp_path / "shim-roots" / "state" / "shim"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)  # the journal refuses a directory anyone else can read
    path = root / "ledger-2026-09.jsonl"
    path.write_text(
        json.dumps(
            {"ts": "2026-09-01T00:00:00Z", "entities": {"IBAN": 2}, "tool_name": "Read"}
        )
        + "\n"
        + json.dumps(
            {
                "ts": "2026-09-02T00:00:00Z",
                "entities": {"EMAIL": 1},
                "tool_name": "Read",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)

    result = runner.invoke(app, ["ledger", "show"])
    text = " ".join(unstyle(result.output).split())

    assert result.exit_code == 0
    assert "2 events over 2 day(s)" in text
    assert f"kept for {ledger.RETENTION_DAYS} days" in text
    assert "2026-09-01 1 event 2 IBAN" in text
    assert "2026-09-02 1 event 1 EMAIL" in text

    payload = json.loads(runner.invoke(app, ["ledger", "show", "--json"]).output)
    assert payload["events"] == 2
    assert payload["days"] == 2
    assert [entry["ts"] for entry in payload["entries"]] == [
        "2026-09-01T00:00:00Z",
        "2026-09-02T00:00:00Z",
    ]


def test_an_empty_ledger_says_how_to_turn_it_on(monkeypatch, tmp_path) -> None:
    result = runner.invoke(app, ["ledger", "show"])

    assert result.exit_code == 0
    assert "shim config --ledger" in unstyle(result.output)


def test_a_failed_removal_is_not_reported_as_a_removal(monkeypatch, tmp_path) -> None:
    """`removed the old hook file at ...` printed even when the unlink raised,
    because the message sat outside the suppress. The file was still there and
    doctor kept naming it, while the user had been told it was gone.
    """
    from shim_cli.cli import integrations

    legacy = tmp_path / "shim-guard.json"
    legacy.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(integrations, "_legacy_copilot_file", lambda: legacy)

    def refuse(self):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "unlink", refuse)
    printed: list[str] = []
    monkeypatch.setattr(
        integrations, "emit", lambda level, text, **kw: printed.append(text)
    )

    integrations._remove_legacy_copilot_file()

    assert printed == [], "nothing was removed, so nothing may say it was"


def test_each_unsafe_settings_state_names_its_own_cause(
    monkeypatch, tmp_path: Path
) -> None:
    """`shim settings cannot be read safely` covers about a dozen checks —
    symlink, hard link, ownership, size, changed mid-read. Collapsing them all
    into one permissions message sends someone to chmod a file that is already
    0600 when the real problem is that it is a symlink.
    """
    home = tmp_path / "cfg"
    (home / "shim").mkdir(parents=True)
    home.chmod(0o700)
    target = home / "shim" / "config.toml"

    def settings_error() -> str:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
        return " ".join(unstyle(runner.invoke(app, ["config"]).output).split())

    real = home / "real.toml"
    real.write_text('enabled_entities = ["SECRET"]\n', encoding="utf-8")
    real.chmod(0o600)
    target.symlink_to(real)
    assert "must not be a symlink" in settings_error()

    target.unlink()
    target.write_text('enabled_entities = ["SECRET"]\n', encoding="utf-8")
    target.chmod(0o600)
    (home / "shim" / "other.toml").hardlink_to(target)
    assert "must not be hard-linked" in settings_error()

    (home / "shim" / "other.toml").unlink()
    text = settings_error()
    assert "refused" not in text, "a private, regular, single-linked file is fine"

    (home / "shim").chmod(0o777)
    writable = settings_error()
    assert "writable by another user" in writable
    # Only this family gets the explanation; a symlink does not need it.
    assert "turn detection off" in writable
