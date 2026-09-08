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

PLUGIN_ROOT = Path(__file__).parents[2] / "plugins" / "shim-guard"
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
