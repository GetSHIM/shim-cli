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


@pytest.mark.parametrize(
    ("client", "manifest", "settings"),
    [
        ("claude", "claude.json", claude_settings),
        ("codex", "hooks.json", codex_settings),
    ],
)
def test_the_plugin_and_the_installer_register_the_same_events(
    client: str, manifest: str, settings: object
) -> None:
    document = json.loads(
        (PLUGIN_ROOT / "hooks" / manifest).read_text(encoding="utf-8")
    )
    expected = {event for event, _group in settings.hook_groups()}

    assert set(document["hooks"]) == expected


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

    check = diagnostics._duplicate_check("claude")

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
    for step in (
        "`shim-hook` on `PATH`",
        "`shim-guard-hook` on `PATH`",
        "`<plugin-root>/bin/shim.pyz`",
    ):
        assert step in text, step
    assert text.index("`shim-hook` on `PATH`") < text.index(
        "`shim-guard-hook` on `PATH`"
    )
    assert "Codex sets no such variable" in text


def _codex_command() -> str:
    document = json.loads(
        (PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8")
    )
    return document["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]


def test_the_codex_command_stands_down_under_claude_code() -> None:
    """Claude Code loads hooks/hooks.json by convention, on top of the
    hooks/claude.json its plugin.json declares, and does not expand
    ${PLUGIN_ROOT}. Without this guard every Claude prompt ran `/hooks/run-shim`
    and logged exit 127 next to the real hook's output.
    """
    assert '[ -n "$CLAUDE_PLUGIN_ROOT" ] && exit 0' in _codex_command()


def test_the_codex_command_exits_silently_when_claude_code_runs_it() -> None:
    import subprocess

    result = subprocess.run(
        ("/bin/sh", "-c", _codex_command()),
        input=b'{"hook_event_name":"UserPromptSubmit","prompt":"AKIAIOSFODNN7EXAMPLE"}',
        capture_output=True,
        env={"CLAUDE_PLUGIN_ROOT": "/x", "PATH": "/usr/bin:/bin"},
        check=False,
        timeout=60,
    )

    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr == b""


def test_the_codex_command_still_runs_the_launcher_under_codex(tmp_path) -> None:
    """Codex substitutes ${PLUGIN_ROOT} itself and sets no CLAUDE_PLUGIN_ROOT."""
    import os
    import subprocess

    command = _codex_command().replace("${PLUGIN_ROOT}", str(PLUGIN_ROOT))
    environment = {
        key: value for key, value in os.environ.items() if key != "CLAUDE_PLUGIN_ROOT"
    } | {"TMPDIR": str(tmp_path)}

    result = subprocess.run(
        ("/bin/sh", "-c", command),
        input=b'{"hook_event_name":"UserPromptSubmit","prompt":"AKIAIOSFODNN7EXAMPLE"}',
        capture_output=True,
        env=environment,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0
    assert b"SECRET" in result.stdout
