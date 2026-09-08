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
