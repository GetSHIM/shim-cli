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
from shim_cli.session.ledger import LedgerError
from shim_cli.session.spool import SpoolError

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
    monkeypatch.setenv("SHIM_CONFIG", str(target))
    return target


TAGLINE = "Local traffic visibility and privacy controls for coding agents."


@pytest.mark.parametrize("args", (["help"], ["--help"]), ids=("help", "--help"))
def test_help_opens_with_the_readme_tagline(args: list[str]) -> None:
    result = runner.invoke(app, args)

    assert result.exit_code == 0
    assert TAGLINE in " ".join(unstyle(result.output).split())


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
    assert "Current detection: 12/13 enabled" in initial.output
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


def test_codex_install_says_where_the_hook_is_trusted(
    monkeypatch, tmp_path: Path
) -> None:
    _codex_home(monkeypatch, tmp_path)

    installed = runner.invoke(app, ["install", "codex", "--yes"])
    said = " ".join(unstyle(installed.output).split())

    assert installed.exit_code == 0
    assert (
        "WARN Codex skips a hook you have not trusted, without warning: "
        "open /hooks in Codex, review the shim entry and enable it." in said
    )
    assert "when asked" not in said


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
    assert json.loads(doctor.output)["status"] == "ok"
    # The file is shim's own, so revert deletes it rather than leaving an
    # empty `{"version": 1, "hooks": {}}` shell behind.
    assert not target.exists()
    assert json.loads(preview.output[preview.output.index("{") :]) == hook_document()


def test_copilot_doctor_names_no_trust_step_and_no_double_inspection(
    monkeypatch, tmp_path: Path
) -> None:
    _copilot_home(monkeypatch, tmp_path)
    _copilot(monkeypatch, tmp_path)
    runner.invoke(app, ["install", "copilot", "--yes"])

    said = " ".join(unstyle(runner.invoke(app, ["doctor", "copilot"]).output).split())
    checks = {
        item["name"]: item["status"]
        for item in json.loads(
            runner.invoke(app, ["doctor", "copilot", "--json"]).output
        )["checks"]
    }

    assert (
        "PASS GitHub Copilot CLI has no trust step; the hook runs from the next session."
        in said
    )
    assert (
        "PASS The shim plugin stands down in GitHub Copilot CLI, so nothing is inspected twice."
        in said
    )
    assert "/hooks" not in said
    assert checks["hook_activation"] == checks["duplicate_hooks"] == "PASS"


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


_CLIENT_FIXTURES = {
    "claude": (_claude_home, _claude, 6),
    "codex": (_codex_home, _codex, 1),
    "copilot": (_copilot_home, _copilot, 1),
}


def _coverage(client: str) -> tuple[str, list[str], list[bool]]:
    text = " ".join(unstyle(runner.invoke(app, ["doctor", client]).output).split())
    rows = json.loads(runner.invoke(app, ["doctor", client, "--json"]).output)
    table = text[text.index("coverage Event") :].split()
    return (
        text,
        [word for word in table if word in ("yes", "no")][1::2],
        [row["installed"] for row in rows["coverage"]],
    )


@pytest.mark.parametrize("client", sorted(_CLIENT_FIXTURES))
def test_doctor_coverage_reads_the_hook_file(
    client: str, monkeypatch, tmp_path: Path
) -> None:
    make_home, make_client, events = _CLIENT_FIXTURES[client]
    make_home(monkeypatch, tmp_path)
    make_client(monkeypatch, tmp_path)

    before, before_table, before_json = _coverage(client)
    assert runner.invoke(app, ["install", client, "--yes"]).exit_code == 0
    after, after_table, after_json = _coverage(client)

    assert (
        f"WARN Coverage: 0 of {events} events installed; run shim install {client}."
        in before
    )
    assert before_table == ["no"] * events
    assert before_json == [False] * events
    assert f"PASS Coverage: {events} of {events} events installed." in after
    assert after_table == ["yes"] * events
    assert after_json == [True] * events


@pytest.mark.parametrize("client", sorted(_CLIENT_FIXTURES))
def test_doctor_fails_a_020_hook_that_1_0_does_not_run(
    client: str, monkeypatch, tmp_path: Path
) -> None:
    from shim_cli.cli.integrations import client_plan
    from shim_cli.clients.claude import settings as claude_settings
    from shim_cli.clients.codex import settings as codex_settings
    from shim_cli.clients.copilot import settings as copilot_settings
    from shim_cli.clients.hook_settings import add_groups

    make_home, make_client, events = _CLIENT_FIXTURES[client]
    make_home(monkeypatch, tmp_path)
    make_client(monkeypatch, tmp_path)
    if client == "copilot":
        legacy = copilot_settings.legacy_target_path()
        legacy.parent.mkdir(parents=True)
        legacy.write_text(
            json.dumps(
                copilot_settings.hook_document(
                    module=copilot_settings.LEGACY_HOOK_MODULE
                ),
                indent=2,
            )
            + "\n"
        )
    else:
        module = {"claude": claude_settings, "codex": codex_settings}[client]
        client_plan(client, "install").target.write_bytes(
            add_groups(b"{}", module.legacy_hook_groups())
        )

    text, table, installed = _coverage(client)

    assert (
        f"WARN Coverage: 0 of {events} events installed; run shim install {client}."
        in text
    )
    assert table == ["no"] * events
    assert installed == [False] * events
    assert (
        f"FAIL hook installed in the 0.2.0 shape, which 1.0 does not run; "
        f"run shim install {client}." in text
    )
    assert "hook group is not installed" not in text
    assert runner.invoke(app, ["doctor", client]).exit_code == 2


def test_doctor_not_installed_names_the_command(monkeypatch, tmp_path: Path) -> None:
    _claude_home(monkeypatch, tmp_path)
    _claude(monkeypatch, tmp_path)

    text = " ".join(unstyle(runner.invoke(app, ["doctor", "claude"]).output).split())

    assert (
        "WARN shim's Claude Code hook group is not installed; run shim install claude."
        in text
    )


def test_doctor_without_codex_says_each_fact_once(monkeypatch, tmp_path: Path) -> None:
    _codex_home(monkeypatch, tmp_path)
    monkeypatch.setenv("PATH", "")

    text = " ".join(unstyle(runner.invoke(app, ["doctor", "codex"]).output).split())
    payload = json.loads(runner.invoke(app, ["doctor", "codex", "--json"]).output)
    checks = {item["name"]: item["status"] for item in payload["checks"]}

    assert text.count("Codex executable was not found on PATH.") == 1
    assert (
        text.count("Codex hook support was not checked: the executable was not found.")
        == 1
    )
    assert checks["codex"] == checks["hooks_feature"] == "FAIL"


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

    result = runner.invoke(app, ["report"])

    assert result.exit_code == 1
    assert "No session on record" in result.output


def test_report_renders_the_most_recent_session(monkeypatch, tmp_path: Path) -> None:
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
    monkeypatch.setenv("SHIM_CONFIG", str(target))
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
    monkeypatch.setenv("SHIM_CONFIG", str(target))
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

    rows = {row["event"]: row for row in _coverage_rows("claude", frozenset({"Stop"}))}

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


def test_adding_a_pattern_enables_custom_on_a_0_2_0_entity_list(
    monkeypatch, tmp_path
) -> None:
    from shim_cli.guard import BUILT_IN_TYPES

    target = _guard_config(monkeypatch, tmp_path)
    target.parent.mkdir(parents=True)
    listed = ", ".join(f'"{name}"' for name in BUILT_IN_TYPES)
    target.write_text(f"enabled_entities = [{listed}]\n", encoding="utf-8")
    target.chmod(0o600)

    runner.invoke(app, ["config", "--custom", r"CODENAME=\bATLAS-[0-9]{4}\b", "--yes"])
    scanned = runner.invoke(app, ["scan", "--json"], input="ship ATLAS-0042")
    runner.invoke(
        app,
        [
            "config",
            "--disable",
            "CUSTOM",
            "--custom-literal",
            "HOST=db-core-01",
            "--yes",
        ],
    )
    disabled = runner.invoke(app, ["scan", "--json"], input="ship ATLAS-0042")

    assert json.loads(scanned.output)["counts"] == {"CUSTOM": 1}
    assert json.loads(disabled.output)["counts"] == {}


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


def test_doctor_names_an_unreadable_settings_file_once(monkeypatch, tmp_path) -> None:
    target = _guard_config(monkeypatch, tmp_path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text('enabled_entities = ["EMAIL"\n', encoding="utf-8")
    target.chmod(0o600)

    result = runner.invoke(app, ["doctor", "claude", "--json"])

    checks = {item["name"]: item for item in json.loads(result.output)["checks"]}
    assert checks["entity_settings"]["status"] == "FAIL"
    assert "custom_patterns" not in checks


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
    from shim_cli.cli.diagnostics import _runner_check

    target = tmp_path / "settings" / "config.toml"
    target.parent.mkdir(mode=0o700, parents=True)
    target.write_text('[mode]\nuser-prompt = "enforce"\n', encoding="utf-8")
    target.chmod(0o600)
    monkeypatch.setenv("SHIM_CONFIG", str(target))

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

    check = diagnostics._resolution_check("codex", frozenset({"UserPromptSubmit"}))

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

    assert diagnostics._resolution_check("codex", frozenset()).status == "FAIL"


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
    home = _claude_home(monkeypatch, tmp_path)
    _claude(monkeypatch, tmp_path)
    _legacy_claude_settings(home)

    result = runner.invoke(app, ["doctor", "claude"])
    text = " ".join(unstyle(result.output).split())

    assert (
        "FAIL hook installed in the 0.2.0 shape, which 1.0 does not run; "
        "run shim install claude." in text
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


def test_an_empty_ledger_that_is_on_does_not_say_turn_it_on(
    monkeypatch, tmp_path
) -> None:
    _guard_config(monkeypatch, tmp_path)
    runner.invoke(app, ["config", "--ledger", "--yes"])

    result = runner.invoke(app, ["ledger", "show"])

    assert result.exit_code == 0
    assert "The ledger is on and has recorded nothing yet." in unstyle(result.output)
    assert "shim config --ledger" not in unstyle(result.output)


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

    assert integrations._remove_legacy_copilot_file(False) is False
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


def _five_of_six(monkeypatch, tmp_path: Path) -> Path:
    from shim_cli.clients.claude import settings as claude_settings
    from shim_cli.clients.hook_settings import add_groups

    home = _claude_home(monkeypatch, tmp_path)
    _claude(monkeypatch, tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin{os.pathsep}/bin")
    claude_settings.target_path().write_bytes(
        add_groups(
            None,
            [
                registration
                for registration in claude_settings.hook_groups()
                if registration[0] != "PostToolUseFailure"
            ],
        )
    )
    return home


def test_doctor_on_a_1_0_2_install_without_shim_hook_on_path_still_passes_resolution(
    monkeypatch, tmp_path: Path
) -> None:
    _five_of_six(monkeypatch, tmp_path)

    result = runner.invoke(app, ["doctor", "claude"])
    text = " ".join(unstyle(result.output).split())

    assert "PASS The installed Claude Code hook runs this package directly" in text
    assert "No hook is runnable" not in text
    assert result.exit_code == 0


def test_doctor_finds_a_plugin_beside_a_1_0_2_install(
    monkeypatch, tmp_path: Path
) -> None:
    home = _five_of_six(monkeypatch, tmp_path)
    manifest = home / ".claude" / "plugins" / "installed_plugins.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"plugins": {"shim-cli@shim-cli": [{"version": "1.0.2"}]}}),
        encoding="utf-8",
    )

    text = " ".join(unstyle(runner.invoke(app, ["doctor", "claude"]).output).split())

    assert (
        "FAIL Both the shim-cli@shim-cli plugin and a settings hook are installed"
        in text
    )


def test_doctor_counts_a_1_0_2_install_as_five_of_six(
    monkeypatch, tmp_path: Path
) -> None:
    from shim_cli.clients.claude import settings as claude_settings
    from shim_cli.clients.hook_settings import add_groups

    _claude_home(monkeypatch, tmp_path)
    _claude(monkeypatch, tmp_path)
    claude_settings.target_path().write_bytes(
        add_groups(
            None,
            [
                registration
                for registration in claude_settings.hook_groups()
                if registration[0] != "PostToolUseFailure"
            ],
        )
    )

    text, table, installed = _coverage("claude")
    rows = json.loads(runner.invoke(app, ["doctor", "claude", "--json"]).output)

    assert "WARN Coverage: 5 of 6 events installed; run shim install claude." in text
    assert "hook group is not installed" not in text
    assert "PostToolUseFailure error no no" in text
    assert installed == [
        row["event"] != "PostToolUseFailure" for row in rows["coverage"]
    ]
    assert runner.invoke(app, ["doctor", "claude"]).exit_code == 0
    assert runner.invoke(app, ["install", "claude", "--yes"]).exit_code == 0
    assert "PASS Coverage: 6 of 6 events installed." in _coverage("claude")[0]


def test_scan_and_redact_fail_when_part_of_the_input_cannot_be_read() -> None:
    text = (
        "x" * 90_000 + "\n" + "\ufdfa" * 12_000 + "\nDB_PASSWORD=Synthetic-pass-0000\n"
    )

    scan = runner.invoke(app, ["scan"], input=text)
    redact = runner.invoke(app, ["redact"], input=text)

    assert (scan.exit_code, redact.exit_code) == (1, 1)
    assert "Unable to process stdin" in scan.output
    assert "Synthetic-pass" not in redact.output


WINDOWS_REFUSAL = (
    "FAIL shim-cli does not support Windows yet. Run it inside WSL, or on macOS or "
    "Linux."
)


@pytest.mark.parametrize(
    "arguments",
    (
        ["install", "claude", "--yes"],
        ["revert", "claude", "--yes"],
        ["doctor", "claude"],
        ["status", "claude"],
        ["report"],
        ["ledger", "show"],
        ["ledger", "purge", "--yes"],
        ["watch", "--", "claude"],
        ["config", "--only", "EMAIL", "--yes"],
        ["scan"],
        ["redact"],
        ["demo", "claude"],
        ["audit"],
        ["audit", "--purge"],
        ["keys", ".env"],
    ),
)
def test_on_windows_every_command_refuses_before_touching_a_file(
    arguments: list, monkeypatch, tmp_path: Path
) -> None:
    import shim_cli

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(shim_cli, "WINDOWS", True)
    before = set(tmp_path.rglob("*"))

    result = runner.invoke(app, arguments, input="Contact alice@example.com")

    assert result.exit_code == 2
    assert WINDOWS_REFUSAL in " ".join(unstyle(result.stderr).split())
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("arguments", (["help"], ["--version"], []))
def test_on_windows_help_and_version_still_answer(arguments: list, monkeypatch) -> None:
    import shim_cli

    monkeypatch.setattr(shim_cli, "WINDOWS", True)

    result = runner.invoke(app, arguments)

    assert result.exit_code == 0
    assert "Windows" not in result.output


# Every error code a command emits, driven through the command (R3 of the
# error-code change). `human` is the stderr each case printed before codes
# existed, kept byte for byte; None where the human path asks instead.


def _valid_settings(monkeypatch, tmp_path: Path) -> dict:
    target = _guard_config(monkeypatch, tmp_path)
    target.parent.mkdir(mode=0o700)
    target.write_text('enabled_entities = ["EMAIL"]\n')
    target.chmod(0o600)
    return {"path": str(target)}


def _broken_settings(monkeypatch, tmp_path: Path) -> dict:
    from shim_cli.config import tomllib

    found = _valid_settings(monkeypatch, tmp_path)
    Path(found["path"]).write_bytes(b"not toml")
    try:
        tomllib.loads("not toml")
    except tomllib.TOMLDecodeError as error:
        return {**found, "parser": str(error)}
    raise AssertionError("the fixture must not parse")


def _shared_settings(monkeypatch, tmp_path: Path) -> dict:
    found = _valid_settings(monkeypatch, tmp_path)
    Path(found["path"]).parent.chmod(0o777)
    return found


def _relative_settings(monkeypatch, tmp_path: Path) -> dict:
    monkeypatch.setenv("SHIM_CONFIG", "relative/config.toml")
    return {}


def _fails(target: str, error: type[BaseException] = OSError):
    def setup(monkeypatch, tmp_path: Path) -> dict:
        found = _valid_settings(monkeypatch, tmp_path)
        home = _claude_home(monkeypatch, tmp_path)

        def fail(*_args, **_kwargs):
            raise error("synthetic")

        monkeypatch.setattr(target, fail)
        return {**found, "settings": str(home / ".claude" / "settings.json")}

    return setup


def _claude_settings(content: bytes | None, link: bool = False):
    def setup(monkeypatch, tmp_path: Path) -> dict:
        _valid_settings(monkeypatch, tmp_path)
        home = _claude_home(monkeypatch, tmp_path)
        target = home / ".claude" / "settings.json"
        if link:
            real = tmp_path / "real-settings.json"
            real.write_text("{}")
            target.symlink_to(real)
        elif content is not None:
            target.write_bytes(content)
        return {"settings": str(target)}

    return setup


def _history(monkeypatch, tmp_path: Path) -> dict:
    _valid_settings(monkeypatch, tmp_path)
    root = tmp_path / "claude-config"
    (root / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    return {"history": str(root / "projects")}


def _missing_history(monkeypatch, tmp_path: Path) -> dict:
    _valid_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "missing"))
    return {"history": str(tmp_path / "missing" / "projects")}


def _unreadable_history(monkeypatch, tmp_path: Path) -> dict:
    from shim_cli.cli import audit

    found = _history(monkeypatch, tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError("synthetic")

    monkeypatch.setattr(audit, "_scan", fail)
    return found


def _no_terminal(monkeypatch, tmp_path: Path) -> dict:
    from shim_cli.cli import audit

    monkeypatch.setattr(audit, "_terminal", lambda: False)
    return {}


def _keys_file(content: bytes | None, directory: bool = False):
    def setup(monkeypatch, tmp_path: Path) -> dict:
        _valid_settings(monkeypatch, tmp_path)
        target = tmp_path / "keys.env"
        if directory:
            target.mkdir()
        elif content is not None:
            target.write_bytes(content)
        return {"file": str(target)}

    return setup


def _watch(*, claude: bool = True, base_url: bool = False, proxy: str = ""):
    def setup(monkeypatch, tmp_path: Path) -> dict:
        from shim_cli.cli import watch
        from shim_cli.watch import proxy as watch_proxy

        _valid_settings(monkeypatch, tmp_path)
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        monkeypatch.setenv("PATH", str(bin_dir))
        if claude:
            executable = bin_dir / "claude"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
        if base_url:
            monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")

        def refuse(*_args, **_kwargs):
            raise OSError("synthetic")

        if proxy == "fails":
            monkeypatch.setattr(watch_proxy, "start", refuse)
        if proxy == "client fails":
            running = SimpleNamespace(
                base_url="http://127.0.0.1:9", stop=lambda: None, session=None
            )
            monkeypatch.setattr(watch_proxy, "start", lambda *_args: running)
            monkeypatch.setattr(watch.subprocess, "Popen", refuse)
        return {}

    return setup


_FIX_SETTINGS = (
    "Fix the line the error names, or run shim config --reset --yes "
    "(it discards every setting)."
)
_FIX_CHMOD = "chmod 700 ~/.config/shim && chmod 600 ~/.config/shim/config.toml"
_REFUSED_SETTINGS = (
    "Settings at {path} were refused: target parent is writable by another user. "
    "shim will not read settings anything else can rewrite, because whatever can "
    "rewrite them can turn detection off."
)
_INVALID_SETTINGS = (
    "Settings at {path} are invalid: {parser}. "
    "Run shim config --reset to start over, or edit the line above."
)

# code: (argv, setup, exit code, error, fix, extra JSON keys, human stderr)
_ERROR_CASES: dict[str, tuple] = {
    "CONFIRMATION_REQUIRED": (
        ["config", "--enable", "PHONE"],
        _valid_settings,
        2,
        "--yes is required with --json",
        "Add --yes to apply without a question.",
        {},
        None,
    ),
    "OPTIONS_CONFLICT": (
        ["config", "--enable", "EMAIL", "--disable", "EMAIL"],
        _valid_settings,
        2,
        "The same entity cannot be enabled and disabled.",
        "Run the command with one of them.",
        {},
        "FAIL The same entity cannot be enabled and disabled.\n",
    ),
    "TERMINAL_REQUIRED": (
        ["audit", "--purge"],
        _no_terminal,
        2,
        None,
        None,
        {},
        "shim: --purge needs a terminal; nothing was deleted.\n",
    ),
    "SETTINGS_PATH_INVALID": (
        ["config"],
        _relative_settings,
        2,
        "Entity settings path is invalid.",
        "Unset SHIM_CONFIG or set it to an absolute path.",
        {},
        "FAIL Entity settings path is invalid.\n",
    ),
    "SETTINGS_PATH_UNSAFE": (
        ["config", "--enable", "PHONE", "--yes"],
        _shared_settings,
        2,
        "Entity settings path is unsafe; nothing was saved.",
        _FIX_CHMOD,
        {},
        "FAIL Entity settings path is unsafe; nothing was saved.\n",
    ),
    "SETTINGS_INVALID": (
        ["config"],
        _broken_settings,
        2,
        _INVALID_SETTINGS,
        _FIX_SETTINGS,
        {},
        f"FAIL {_INVALID_SETTINGS}\n",
    ),
    "SETTINGS_REFUSED": (
        ["config"],
        _shared_settings,
        2,
        _REFUSED_SETTINGS,
        _FIX_CHMOD,
        {},
        f"FAIL {_REFUSED_SETTINGS}\n",
    ),
    "CUSTOM_PATTERN_INVALID": (
        ["config", "--custom", "LOOP=(a+)+$", "--yes"],
        _valid_settings,
        2,
        "pattern LOOP backtracks on repeated input; simplify it",
        "Change the pattern, or remove it with shim config --remove-custom NAME --yes.",
        {},
        "FAIL pattern LOOP backtracks on repeated input; simplify it\n",
    ),
    "REVEAL_INVALID": (
        ["config", "--reveal", "SECRET=4", "--yes"],
        _valid_settings,
        2,
        "that entity cannot reveal a tail",
        "Use --reveal IBAN=N, CREDIT_CARD=N or PHONE=N with N from 1 to 4.",
        {},
        "FAIL that entity cannot reveal a tail\n",
    ),
    "SETTINGS_CHANGED": (
        ["config", "--enable", "PHONE", "--yes"],
        _fails("shim_cli.cli.configuration.apply"),
        2,
        "Entity settings were unsafe or changed; nothing was saved.",
        "Run the command again.",
        {},
        "FAIL Entity settings were unsafe or changed; nothing was saved.\n",
    ),
    "CLIENT_SETTINGS_UNREADABLE": (
        ["status", "claude"],
        _fails("shim_cli.cli.integrations.client_plan"),
        2,
        "Unable to inspect Claude Code hook configuration.",
        "Run shim doctor claude.",
        {"client": "claude"},
        "FAIL Unable to inspect Claude Code hook configuration.\n",
    ),
    "CLIENT_SETTINGS_MALFORMED": (
        ["install", "claude", "--yes"],
        _claude_settings(b'{"hooks":'),
        2,
        "Claude Code hook configuration cannot be changed safely.",
        "Fix {settings} by hand so it parses, then run the command again.",
        {"client": "claude", "target": "{settings}"},
        "FAIL Claude Code hook configuration cannot be changed safely.\n"
        "WARN Review Claude Code hooks manually; shim did not change malformed, "
        "ambiguous, or unsafe settings.\n",
    ),
    "CLIENT_SETTINGS_UNSAFE": (
        ["revert", "claude", "--yes"],
        _claude_settings(None, link=True),
        2,
        "Claude Code hook configuration cannot be removed safely.",
        "Make {settings} a regular file owned by you, then run the command again.",
        {"client": "claude", "target": "{settings}"},
        "FAIL Claude Code hook configuration cannot be removed safely.\n"
        "WARN Review Claude Code hooks manually; shim removes only its exact hook "
        "group.\n",
    ),
    "CLIENT_SETTINGS_CHANGED": (
        ["install", "claude", "--yes"],
        _fails("shim_cli.cli.integrations.apply"),
        2,
        "Claude Code hook configuration was not changed.",
        "Run the command again.",
        {"client": "claude"},
        "FAIL Claude Code hook configuration was not changed.\n",
    ),
    "DETECTOR_UNAVAILABLE": (
        ["install", "claude", "--yes"],
        _fails("shim_cli.guard.evaluate", RuntimeError),
        2,
        "shim detector could not start.",
        "Reinstall shim: uv tool install --reinstall shim, or pipx reinstall shim.",
        {"client": "claude"},
        "FAIL shim detector could not start.\n",
    ),
    "INVALID_DATE": (
        ["audit", "--since", "2026/09/01"],
        _history,
        2,
        "--since takes a date as YYYY-MM-DD",
        "Pass --since as YYYY-MM-DD.",
        {},
        "shim: --since takes a date as YYYY-MM-DD.\n",
    ),
    "HISTORY_NOT_FOUND": (
        ["audit"],
        _missing_history,
        2,
        "no Claude Code history at {history}",
        None,
        {},
        "shim: no Claude Code history at {history}.\n",
    ),
    "HISTORY_UNREADABLE": (
        ["audit"],
        _unreadable_history,
        2,
        "the Claude Code history at {history} could not be read",
        None,
        {},
        "shim: the Claude Code history at {history} could not be read.\n",
    ),
    "RECORDS_UNREADABLE": (
        ["report"],
        _fails("shim_cli.session.spool.newest", SpoolError),
        2,
        "Session records could not be read.",
        "Run shim doctor claude (or codex, copilot); its session_record line "
        "names the cause.",
        {},
        "FAIL Session records could not be read.\n",
    ),
    "LEDGER_UNREADABLE": (
        ["ledger", "show"],
        _fails("shim_cli.session.ledger.entries", LedgerError),
        2,
        "The ledger could not be read.",
        "Run shim doctor claude (or codex, copilot).",
        {},
        "FAIL The ledger could not be read.\n",
    ),
    "FILE_NOT_FOUND": (
        ["keys", "{file}"],
        _keys_file(None),
        2,
        "{file} does not exist",
        None,
        {},
        "shim: {file} does not exist.\n",
    ),
    "NOT_A_FILE": (
        ["keys", "{file}"],
        _keys_file(None, directory=True),
        2,
        "{file} is not a regular file",
        None,
        {},
        "shim: {file} is not a regular file.\n",
    ),
    "FILE_TOO_LARGE": (
        ["keys", "{file}"],
        _keys_file(b"A=1\n" * 262_145),
        2,
        "{file} is larger than 1 MB",
        None,
        {},
        "shim: {file} is larger than 1 MB.\n",
    ),
    "NOT_UTF8": (
        ["keys", "{file}"],
        _keys_file(b"A=\xff\n"),
        2,
        "{file} is not UTF-8 text",
        None,
        {},
        "shim: {file} is not UTF-8 text.\n",
    ),
    "STDIN_UNPROCESSABLE": (
        ["scan"],
        _valid_settings,
        1,
        "Unable to process stdin.",
        "Pipe UTF-8 text; if the settings file is refused, shim config prints why.",
        {},
        "FAIL Unable to process stdin.\n",
    ),
    "NOTHING_TO_RUN": (
        ["watch"],
        _watch(),
        2,
        "Nothing to run. Try: shim watch -- claude",
        "shim watch -- claude",
        {},
        "FAIL Nothing to run. Try: shim watch -- claude\n",
    ),
    "CLIENT_UNSUPPORTED": (
        ["watch", "--", "codex"],
        _watch(),
        2,
        "shim watch does not support codex. Codex takes its endpoint from its own "
        "configuration, so the proxy would be bypassed and the session measured as "
        "empty. The Codex prompt hook is unaffected.",
        "shim install codex installs the hook, which needs no proxy.",
        {},
        "FAIL shim watch does not support codex. Codex takes its endpoint from its "
        "own configuration, so the proxy would be bypassed and the session measured "
        "as empty. The Codex prompt hook is unaffected.\n",
    ),
    "BASE_URL_ALREADY_SET": (
        ["watch", "--", "claude"],
        _watch(base_url=True),
        2,
        "ANTHROPIC_BASE_URL is already configured; custom upstreams are "
        "unsupported. Run shim watch with ANTHROPIC_BASE_URL unset, or use the hook "
        "(shim install claude), which does not need the proxy.",
        "Unset ANTHROPIC_BASE_URL for this command.",
        {},
        "FAIL ANTHROPIC_BASE_URL is already configured; custom upstreams are "
        "unsupported. Run shim watch with ANTHROPIC_BASE_URL unset, or use the hook "
        "(shim install claude), which does not need the proxy.\n",
    ),
    "EXECUTABLE_NOT_FOUND": (
        ["watch", "--", "claude"],
        _watch(claude=False),
        2,
        "claude was not found on PATH.",
        None,
        {},
        "FAIL claude was not found on PATH.\n",
    ),
    "PROXY_FAILED": (
        ["watch", "--", "claude"],
        _watch(proxy="fails"),
        2,
        "The proxy could not start (synthetic); nothing was run.",
        None,
        {},
        "FAIL The proxy could not start (synthetic); nothing was run.\n",
    ),
    "CLIENT_START_FAILED": (
        ["watch", "--", "claude"],
        _watch(proxy="client fails"),
        2,
        "claude could not be started (synthetic).",
        None,
        {},
        "FAIL claude could not be started (synthetic).\n",
    ),
}


def _with_json(argv: list[str]) -> list[str]:
    if "--" in argv:
        index = argv.index("--")
        return [*argv[:index], "--json", *argv[index:]]
    return [*argv, "--json"]


def _run_case(code: str, as_json: bool, monkeypatch, tmp_path: Path):
    argv, setup, *_ = _ERROR_CASES[code]
    monkeypatch.setenv("COLUMNS", "1000")
    values = setup(monkeypatch, tmp_path)
    argv = [part.format(**values) for part in argv]
    result = runner.invoke(
        app,
        _with_json(argv) if as_json else argv,
        input=b"\xff" if argv == ["scan"] else None,
    )
    return result, values


@pytest.mark.parametrize(
    "code", sorted(code for code in _ERROR_CASES if code != "TERMINAL_REQUIRED")
)
def test_every_json_error_carries_its_code_and_fix(
    code: str, monkeypatch, tmp_path: Path
) -> None:
    _, _, exit_code, error, fix, extra, _ = _ERROR_CASES[code]

    result, values = _run_case(code, True, monkeypatch, tmp_path)

    assert result.exit_code == exit_code
    argv = _ERROR_CASES[code][0]
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "command": f"ledger-{argv[1]}" if argv[0] == "ledger" else argv[0],
        "status": "error",
        "error": error.format(**values),
        "code": code,
        "fix": fix and fix.format(**values),
        **{key: value.format(**values) for key, value in extra.items()},
    }


@pytest.mark.parametrize(
    "code", sorted(code for code, case in _ERROR_CASES.items() if case[6])
)
def test_human_error_output_is_unchanged(
    code: str, monkeypatch, tmp_path: Path
) -> None:
    _, _, exit_code, *_, human = _ERROR_CASES[code]

    result, values = _run_case(code, False, monkeypatch, tmp_path)

    assert result.exit_code == exit_code
    assert result.stderr == human.format(**values)
    assert not result.stdout.startswith("{")


def test_lt_b24_reveal_error_says_what_is_wrong(monkeypatch, tmp_path: Path) -> None:
    _valid_settings(monkeypatch, tmp_path)

    shown = runner.invoke(app, ["config", "--reveal", "SECRET=4", "--yes"])
    answered = runner.invoke(app, ["config", "--reveal", "SECRET=4", "--json"])

    payload = json.loads(answered.stdout)
    assert payload["code"] == "REVEAL_INVALID"
    assert f"FAIL {payload['error']}" in unstyle(shown.stderr)
    assert "unable to process entity settings" not in answered.stdout


_CLAUDE_EVENTS = [
    "PostToolUse",
    "PostToolUseFailure",
    "PreToolUse",
    "SessionEnd",
    "Stop",
    "UserPromptSubmit",
]


def _json_install(*arguments: str) -> tuple[int, dict]:
    result = runner.invoke(app, ["install", *arguments, "--json"])
    return result.exit_code, json.loads(result.stdout)


def test_install_json_creates_updates_and_does_nothing_twice(
    monkeypatch, tmp_path: Path
) -> None:
    home = _claude_home(monkeypatch, tmp_path)
    target = home / ".claude" / "settings.json"

    created = _json_install("claude", "--yes")
    again = _json_install("claude", "--yes")

    assert created == (
        0,
        {
            "schema_version": 1,
            "command": "install",
            "status": "ok",
            "client": "claude",
            "target": str(target),
            "action": "create",
            "events": _CLAUDE_EVENTS,
            "replaced_legacy": False,
            "preserved_hooks": False,
            "warnings": [],
            "next_step": "Start a new Claude Code session; /hooks lists shim.",
        },
    )
    assert again[1]["action"] == "noop"
    assert again[1]["events"] == []


def test_install_json_names_what_it_kept_and_replaced(
    monkeypatch, tmp_path: Path
) -> None:
    home = _claude_home(monkeypatch, tmp_path)
    _legacy_claude_settings(home, foreign=True)

    code, payload = _json_install("claude", "--yes")

    assert code == 0
    assert payload["action"] == "update"
    assert payload["replaced_legacy"] is True
    assert payload["preserved_hooks"] is True
    assert payload["warnings"] == [
        "Existing Claude Code hooks will be preserved; shim will be appended last."
    ]


def test_install_json_for_codex_names_the_trust_step(
    monkeypatch, tmp_path: Path
) -> None:
    _codex_home(monkeypatch, tmp_path)

    code, payload = _json_install("codex", "--yes")

    assert code == 0
    assert payload["events"] == ["UserPromptSubmit"]
    assert payload["next_step"] == (
        "Codex skips a hook you have not trusted, without warning: open /hooks in "
        "Codex, review the shim entry and enable it."
    )


def test_install_json_without_yes_writes_nothing(monkeypatch, tmp_path: Path) -> None:
    home = _claude_home(monkeypatch, tmp_path)
    target = home / ".claude" / "settings.json"

    refused = runner.invoke(app, ["install", "claude", "--json"])

    assert refused.exit_code == 2
    assert json.loads(refused.stdout)["code"] == "CONFIRMATION_REQUIRED"
    assert json.loads(refused.stdout)["fix"] == "Add --yes to apply without a question."
    assert not target.exists()


def test_install_dry_run_json_returns_the_fragment_and_writes_nothing(
    monkeypatch, tmp_path: Path
) -> None:
    from shim_cli.cli.integrations import _fragment_summary, _hook_fragment

    home = _claude_home(monkeypatch, tmp_path)
    target = home / ".claude" / "settings.json"

    result = runner.invoke(app, ["install", "claude", "--dry-run", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "command": "install",
        "status": "ok",
        "client": "claude",
        "target": str(target),
        "action": "create",
        "events": _CLAUDE_EVENTS,
        "fragment": _hook_fragment("claude"),
        "summary": _fragment_summary("claude"),
        "dry_run": True,
    }
    assert not target.exists()


@pytest.mark.parametrize(
    ("content", "code"),
    [(b'{"hooks":', "CLIENT_SETTINGS_MALFORMED"), (None, "CLIENT_SETTINGS_UNSAFE")],
)
def test_install_json_refuses_a_file_it_cannot_change(
    content: bytes | None, code: str, monkeypatch, tmp_path: Path
) -> None:
    home = _claude_home(monkeypatch, tmp_path)
    target = home / ".claude" / "settings.json"
    if content is None:
        (tmp_path / "real.json").write_text("{}")
        target.symlink_to(tmp_path / "real.json")
    else:
        target.write_bytes(content)
    before = target.read_bytes()

    exit_code, payload = _json_install("claude", "--yes")

    assert exit_code == 2
    assert payload["code"] == code
    assert payload["target"] == str(target)
    assert target.read_bytes() == before


def test_revert_json_removes_does_nothing_and_asks_for_yes(
    monkeypatch, tmp_path: Path
) -> None:
    home = _claude_home(monkeypatch, tmp_path)
    target = home / ".claude" / "settings.json"
    runner.invoke(app, ["install", "claude", "--yes"])
    installed = target.read_bytes()

    refused = runner.invoke(app, ["revert", "claude", "--json"])
    unchanged = target.read_bytes()
    removed = runner.invoke(app, ["revert", "claude", "--yes", "--json"])
    again = runner.invoke(app, ["revert", "claude", "--yes", "--json"])

    assert refused.exit_code == 2
    assert json.loads(refused.stdout)["code"] == "CONFIRMATION_REQUIRED"
    assert unchanged == installed
    assert removed.exit_code == 0
    assert json.loads(removed.stdout) == {
        "schema_version": 1,
        "command": "revert",
        "status": "ok",
        "client": "claude",
        "target": str(target),
        "action": "remove",
        "removed_legacy_file": False,
        "deleted_file": None,
    }
    assert json.loads(again.stdout)["action"] == "noop"


def test_revert_json_names_the_copilot_file_it_deleted(
    monkeypatch, tmp_path: Path
) -> None:
    home = _copilot_home(monkeypatch, tmp_path)
    target = home / ".copilot" / "hooks" / "shim.json"
    runner.invoke(app, ["install", "copilot", "--yes"])

    removed = runner.invoke(app, ["revert", "copilot", "--yes", "--json"])

    assert removed.exit_code == 0
    assert json.loads(removed.stdout)["deleted_file"] == str(target)
    assert not target.exists()


def _doctor_check(code: str, monkeypatch, tmp_path: Path):
    from shim_cli.cli import diagnostics
    from shim_cli.cli.resolution import Resolution
    from shim_cli.session import spool

    def refuse(*_args, **_kwargs):
        raise OSError("synthetic")

    _valid_settings(monkeypatch, tmp_path)
    home = _claude_home(monkeypatch, tmp_path)
    settings = home / ".claude" / "settings.json"
    shim_settings = Path(os.environ["SHIM_CONFIG"])
    if code == "CLIENT_NOT_FOUND":
        monkeypatch.setenv("PATH", "")
        return diagnostics._version_check("claude")
    if code in {"CLIENT_VERSION_UNKNOWN", "CLIENT_TOO_OLD", "CLIENT_NEWER_THAN_TESTED"}:
        version = {
            "CLIENT_VERSION_UNKNOWN": "unknown",
            "CLIENT_TOO_OLD": "0.0.1",
            "CLIENT_NEWER_THAN_TESTED": "99.0.0",
        }[code]
        _claude(monkeypatch, tmp_path, version)
        return diagnostics._version_check("claude")
    if code == "CODEX_HOOKS_DISABLED":
        _codex(monkeypatch, tmp_path)
        (tmp_path / "codex").write_text("#!/bin/sh\nprintf 'hooks stable false\\n'\n")
        return diagnostics._codex_hooks_feature()
    if code == "CODEX_HOOKS_UNCHECKED":
        monkeypatch.setenv("PATH", "")
        return diagnostics._codex_hooks_feature()
    if code == "LEGACY_NAMES_PRESENT":
        return diagnostics._legacy_state("claude", True)
    if code == "CLIENT_SETTINGS_UNREADABLE":
        monkeypatch.setattr(diagnostics, "client_plan", refuse)
        return diagnostics._hook_state("claude")
    if code == "HOOK_NOT_INSTALLED":
        return diagnostics._hook_state("claude")
    if code == "CLIENT_SETTINGS_MALFORMED":
        settings.write_bytes(b'{"hooks":')
        return diagnostics._hook_state("claude")
    if code == "CLIENT_SETTINGS_UNSAFE":
        (tmp_path / "real.json").write_text("{}")
        settings.symlink_to(tmp_path / "real.json")
        return diagnostics._hook_state("claude")
    if code == "SETTINGS_INVALID":
        shim_settings.write_bytes(b"not toml")
        return diagnostics._entity_settings()
    if code == "SETTINGS_REFUSED":
        shim_settings.parent.chmod(0o777)
        return diagnostics._entity_settings()
    if code == "DETECTION_DISABLED":
        shim_settings.write_text("enabled_entities = []\n")
        return diagnostics._entity_settings()
    if code == "CUSTOM_PATTERN_UNSAFE":
        shim_settings.write_text(
            'enabled_entities = ["CUSTOM"]\n\n'
            "[[custom]]\nname = \"LOOP\"\npattern = '(a+)+$'\n"
        )
        return diagnostics._custom_patterns()
    if code == "HOOK_RUNNER_FAILED":
        monkeypatch.setattr(diagnostics, "_run_hook", refuse)
        return diagnostics._runner_check("claude")
    if code in {"HOOK_RESOLUTION_FAILED", "ARCHIVE_VERSION_SKEW"}:
        resolution = (
            Resolution("none", "No hook is runnable.", None, None)
            if code == "HOOK_RESOLUTION_FAILED"
            else Resolution("path", "The package hook is active.", "1.1.0", "1.0.0")
        )
        monkeypatch.setattr(diagnostics, "resolve", lambda: resolution)
        return diagnostics._resolution_check("claude", frozenset())
    if code == "PLUGIN_NOT_DISCOVERABLE":
        return diagnostics._duplicate_check("codex", frozenset())
    if code == "DUPLICATE_HOOKS":
        monkeypatch.setattr(
            diagnostics, "installed_plugins", lambda: [{"key": "shim-cli@shim-cli"}]
        )
        return diagnostics._duplicate_check("claude", frozenset({"Stop"}))
    if code == "SESSION_RECORDS_UNWRITABLE":
        monkeypatch.setattr(spool, "append", refuse)
        return diagnostics._session_record_check()
    if code == "HOOK_EVENTS_MISSING":
        return diagnostics._coverage_check("claude", [{"installed": False}])
    if code == "HOOK_ACTIVATION_UNVERIFIED":
        return diagnostics._activation_check("claude")
    raise AssertionError(f"no fixture for {code}")


_DOCTOR_CODES = (
    "CLIENT_NOT_FOUND",
    "CLIENT_VERSION_UNKNOWN",
    "CLIENT_TOO_OLD",
    "CLIENT_NEWER_THAN_TESTED",
    "CODEX_HOOKS_DISABLED",
    "CODEX_HOOKS_UNCHECKED",
    "LEGACY_NAMES_PRESENT",
    "CLIENT_SETTINGS_UNREADABLE",
    "HOOK_NOT_INSTALLED",
    "CLIENT_SETTINGS_MALFORMED",
    "CLIENT_SETTINGS_UNSAFE",
    "SETTINGS_INVALID",
    "SETTINGS_REFUSED",
    "DETECTION_DISABLED",
    "CUSTOM_PATTERN_UNSAFE",
    "HOOK_RUNNER_FAILED",
    "HOOK_RESOLUTION_FAILED",
    "ARCHIVE_VERSION_SKEW",
    "PLUGIN_NOT_DISCOVERABLE",
    "DUPLICATE_HOOKS",
    "SESSION_RECORDS_UNWRITABLE",
    "HOOK_EVENTS_MISSING",
    "HOOK_ACTIVATION_UNVERIFIED",
)


@pytest.mark.parametrize("code", _DOCTOR_CODES)
def test_every_doctor_problem_names_its_code_and_fix(
    code: str, monkeypatch, tmp_path: Path
) -> None:
    check = _doctor_check(code, monkeypatch, tmp_path)

    assert check is not None
    assert check.status in {"WARN", "FAIL"}
    assert check.code == code
    assert check.fix
    assert "<" not in check.fix, "placeholders are filled at the call site"


def test_doctor_json_carries_detail_code_and_fix(monkeypatch, tmp_path: Path) -> None:
    _claude_home(monkeypatch, tmp_path)
    _claude(monkeypatch, tmp_path)

    before = json.loads(runner.invoke(app, ["doctor", "claude", "--json"]).stdout)
    runner.invoke(app, ["install", "claude", "--yes"])
    after = json.loads(runner.invoke(app, ["doctor", "claude", "--json"]).stdout)

    for payload in (before, after):
        for check in payload["checks"]:
            assert set(check) == {"name", "status", "detail", "code", "fix"}
            if check["status"] == "PASS":
                assert check["code"] is check["fix"] is None
            else:
                assert check["code"] in _DOCTOR_CODES
                assert check["fix"]
    missing = next(c for c in before["checks"] if c["name"] == "coverage")
    assert missing["code"] == "HOOK_EVENTS_MISSING"
    assert missing["fix"] == "Run shim install claude."


def test_the_plate_type_is_listed_off_and_can_be_turned_on(
    monkeypatch, tmp_path: Path
) -> None:
    target = _guard_config(monkeypatch, tmp_path)

    shown = json.loads(runner.invoke(app, ["config", "--json"]).stdout)
    enabled = runner.invoke(
        app, ["config", "--enable", "TR_LICENSE_PLATE", "--yes", "--json"]
    )
    found = runner.invoke(app, ["scan", "--json"], input="Plaka 34 ABC 123")

    assert "TR_LICENSE_PLATE" in shown["disabled_entities"]
    assert "TR_LICENSE_PLATE" in json.loads(enabled.stdout)["enabled_entities"]
    assert "TR_LICENSE_PLATE" in target.read_text()
    assert json.loads(found.stdout)["counts"] == {"TR_LICENSE_PLATE": 1}


def test_config_shows_and_keeps_the_marker_note(monkeypatch, tmp_path: Path) -> None:
    target = _guard_config(monkeypatch, tmp_path)
    target.parent.mkdir()
    target.write_text('enabled_entities = ["EMAIL"]\nmarkers = "note"\n')

    shown = runner.invoke(app, ["config"])
    changed = runner.invoke(app, ["config", "--enable", "PHONE", "--yes", "--json"])
    absent = tmp_path / "other.toml"
    monkeypatch.setenv("SHIM_CONFIG", str(absent))
    default = json.loads(runner.invoke(app, ["config", "--json"]).stdout)

    assert "Markers: note" in shown.output
    assert json.loads(changed.stdout)["markers"] == "note"
    assert 'markers = "note"' in target.read_text()
    assert default["markers"] == "report"
