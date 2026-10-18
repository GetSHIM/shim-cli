from __future__ import annotations

import json
from pathlib import Path

import pytest

import shim_cli
from shim_cli.clients.claude import settings as claude_settings
from shim_cli.clients.codex import settings as codex_settings

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

PLUGIN_ROOT = Path(__file__).parents[2] / "plugins" / "shim-cli"
REPOSITORY_ROOT = PLUGIN_ROOT.parents[1]


def _declared_hooks(host: str) -> Path:
    manifest = json.loads(
        (PLUGIN_ROOT / f".{host}-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    return PLUGIN_ROOT / manifest["hooks"]


def test_plugin_versions_match_package() -> None:
    package = tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    expected = package["project"]["version"]
    assert shim_cli.__version__ == expected

    for host in ("codex", "claude"):
        manifest = json.loads(
            (PLUGIN_ROOT / f".{host}-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        assert manifest["version"] == expected


def test_the_installed_plugin_folder_carries_the_licence() -> None:
    licence = PLUGIN_ROOT / "LICENSE"

    assert licence.is_file()
    assert not licence.is_symlink()
    assert licence.read_bytes() == (REPOSITORY_ROOT / "LICENSE").read_bytes()


@pytest.mark.parametrize(
    ("client", "settings"),
    [("claude", claude_settings), ("codex", codex_settings)],
)
def test_the_plugin_and_the_installer_register_the_same_events(
    client: str, settings: object
) -> None:
    document = json.loads(_declared_hooks(client).read_text(encoding="utf-8"))
    expected = {event for event, _group in settings.hook_groups()}

    assert set(document["hooks"]) == expected


def _registered_commands(client: str) -> list[tuple[str, tuple[str, ...]]]:
    """Every (event, argv) a client runs from this plugin's hook file."""
    if client == "vscode":
        document = json.loads(
            (PLUGIN_ROOT / "com.github.copilot" / "hooks" / "hooks.json").read_text(
                encoding="utf-8"
            )
        )
        handlers = [
            (event, handler)
            for event, entries in document["hooks"].items()
            for handler in entries
        ]
    else:
        document = json.loads(_declared_hooks(client).read_text(encoding="utf-8"))
        handlers = [
            (event, handler)
            for event, groups in document["hooks"].items()
            for group in groups
            for handler in group["hooks"]
        ]
    if client == "claude":
        # Claude Code runs the command itself, with the plugin root substituted.
        return [
            (
                event,
                (
                    handler["command"].replace(
                        "${CLAUDE_PLUGIN_ROOT}", str(PLUGIN_ROOT)
                    ),
                    *handler.get("args", ()),
                ),
            )
            for event, handler in handlers
        ]
    return [
        (event, ("/bin/sh", "-c", handler["command"])) for event, handler in handlers
    ]


@pytest.mark.parametrize("client", ("claude", "codex", "vscode"))
def test_missing_runtime_fails_closed(client: str, tmp_path) -> None:
    """With no Python to run the hook, a prompt is withheld, never sent unread.

    A tool call is denied, as the hook denies one it cannot inspect. Every
    other event is told it was not inspected, on stdout where the client
    shows it, rather than passing in silence.
    """
    import subprocess

    from shim_cli.hook import _DENIED_CALL, _error_output

    shell_only = tmp_path / "bin"
    shell_only.mkdir()
    (shell_only / "sh").symlink_to("/bin/sh")
    environment = {
        "PATH": str(shell_only),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT" if client == "claude" else "PLUGIN_ROOT": str(PLUGIN_ROOT),
    }
    commands = _registered_commands(client)
    assert "UserPromptSubmit" in {event for event, _argv in commands}

    for event, argv in commands:
        result = subprocess.run(
            argv,
            input=json.dumps(
                {"hook_event_name": event, "prompt": "AKIAIOSFODNN7EXAMPLE"}
            ).encode(),
            capture_output=True,
            env=environment,
            check=False,
            timeout=60,
        )

        assert result.returncode == 0, event
        assert b"AKIAIOSFODNN7EXAMPLE" not in result.stdout + result.stderr
        if event == "UserPromptSubmit":
            assert result.stdout == _error_output(client), event
            assert result.stderr == (
                b"shim: no python3 found on PATH; the prompt was withheld.\n"
            )
        elif event == "PreToolUse":
            assert result.stdout == _DENIED_CALL, event
            assert result.stderr == (
                b"shim: no python3 found on PATH; the tool call was not allowed.\n"
            )
        else:
            assert json.loads(result.stdout) == {
                "systemMessage": "shim: no python3 found on PATH; "
                "this event was not inspected."
            }, event
            assert result.stderr == b"", event


def _manifest(home, *keys: str) -> None:
    import json as _json

    target = home / ".claude" / "plugins"
    target.mkdir(parents=True, exist_ok=True)
    (target / "installed_plugins.json").write_text(
        _json.dumps({"plugins": {key: [{"version": "0.3.0"}] for key in keys}}),
        encoding="utf-8",
    )


def test_both_plugin_names_are_discovered(tmp_path) -> None:
    from shim_cli.cli.resolution import installed_plugins

    _manifest(tmp_path, "shim-guard@shim-guard")
    assert [p["key"] for p in installed_plugins(tmp_path)] == ["shim-guard@shim-guard"]

    _manifest(tmp_path, "shim-cli@shim-cli", "shim-guard@shim-guard", "other@market")
    assert sorted(p["key"] for p in installed_plugins(tmp_path)) == [
        "shim-cli@shim-cli",
        "shim-guard@shim-guard",
    ]


def test_a_double_install_is_reported_with_the_uninstall_command(
    tmp_path, monkeypatch
) -> None:
    from shim_cli.cli import diagnostics

    _manifest(tmp_path, "shim-cli@shim-cli", "shim-guard@shim-guard")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))

    check = diagnostics._duplicate_check("claude", frozenset())

    assert check.status == "FAIL"
    assert "inspected twice" in check.detail
    assert "claude plugin uninstall shim-guard@shim-guard" in check.detail


def test_the_plugin_readme_documents_the_launcher_order() -> None:
    from pathlib import Path as _Path

    text = (
        _Path(__file__).parents[2] / "plugins" / "shim-cli" / "README.md"
    ).read_text(encoding="utf-8")

    assert text.startswith("# shim-cli plugin\n")
    assert "`hooks/run-shim <client> [plugin-root]`" in text
    assert "Python 3.9 or newer" in text
    assert text.index("`shim-hook` on `PATH`") < text.index(
        "`<plugin-root>/bin/shim.pyz`"
    )
    assert "Codex sets both" in text


def test_claude_loads_only_commands_the_directory_accepts() -> None:
    loaded = [_declared_hooks("claude")]
    if (PLUGIN_ROOT / "hooks" / "hooks.json").exists():
        loaded.append(PLUGIN_ROOT / "hooks" / "hooks.json")
    prefix = "${CLAUDE_PLUGIN_ROOT}/"

    entries = [
        hook
        for path in loaded
        for groups in json.loads(path.read_text(encoding="utf-8"))["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]

    assert entries
    for hook in entries:
        command, args = hook["command"], hook.get("args", [])
        assert command.startswith(prefix), command
        for part in (command.removeprefix(prefix), *args):
            assert not any(mark in part for mark in ("${", "$(", "`", "*")), part
        assert "-c" not in args
        assert "-c" not in command.split()
    assert not (PLUGIN_ROOT / "hooks" / "hooks.json").exists()


def _codex_command() -> str:
    document = json.loads(_declared_hooks("codex").read_text(encoding="utf-8"))
    return document["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]


def _run_codex_command(command: str, environment: dict, tmp_path) -> object:
    import subprocess

    return subprocess.run(
        ("/bin/sh", "-c", command),
        input=b'{"hook_event_name":"UserPromptSubmit","prompt":"AKIAIOSFODNN7EXAMPLE"}',
        capture_output=True,
        env=environment | {"TMPDIR": str(tmp_path)},
        check=False,
        timeout=120,
    )


@pytest.mark.parametrize("substituted", (True, False), ids=("text", "environment"))
def test_the_codex_command_inspects_the_prompt_under_codex(
    substituted: bool, tmp_path
) -> None:
    import os

    command = _codex_command()
    if substituted:
        command = command.replace("${PLUGIN_ROOT}", str(PLUGIN_ROOT))
    environment = dict(os.environ) | {
        "PLUGIN_ROOT": str(PLUGIN_ROOT),
        "CLAUDE_PLUGIN_ROOT": str(PLUGIN_ROOT),
    }

    result = _run_codex_command(command, environment, tmp_path)

    assert result.returncode == 0
    assert b"SECRET" in result.stdout
