"""The Agent Plugins manifest, and the hook file VS Code reads from it.

The submission checks look for `plugin.json` at the plugin root and accept only
the Agent Plugins v1 fields, so a stray `displayName` or `hooks` key fails the
gate rather than the install. The same file is what Copilot CLI and the Copilot
app read, and those clients run the hook too — hence the stand-down guard.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import shim_cli

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 floor
    import tomli as tomllib

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "shim-cli"
MANIFEST = PLUGIN_ROOT / "plugin.json"
HOOKS = PLUGIN_ROOT / "com.github.copilot" / "hooks" / "hooks.json"
SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
# github/awesome-copilot: eng/external-plugin-quality-gates.mjs
ALLOWED_FIELDS = frozenset(
    {
        "$schema",
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "extensions",
    }
)
PROMPT = (
    b'{"hook_event_name":"UserPromptSubmit","session_id":"p1",'
    b'"prompt":"my key is AKIAIOSFODNN7EXAMPLE"}'
)


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_the_manifest_sits_where_the_submission_checks_look() -> None:
    # plugin.json, .github/plugin/plugin.json and .plugin/plugin.json are the
    # three accepted locations; the gate wants the plugin root itself.
    assert MANIFEST.is_file()


def test_the_manifest_declares_the_agent_plugins_schema() -> None:
    assert _manifest()["$schema"] == SCHEMA_URL


def test_the_manifest_carries_no_field_outside_the_closed_schema() -> None:
    extra = set(_manifest()) - ALLOWED_FIELDS

    assert not extra, f"Agent Plugins v1 rejects {sorted(extra)}"


def test_the_manifest_matches_the_package_version() -> None:
    package = tomllib.loads(
        (PLUGIN_ROOT.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert _manifest()["version"] == package["project"]["version"]
    assert _manifest()["version"] == shim_cli.__version__


def test_the_name_matches_the_marketplace_entries() -> None:
    assert _manifest()["name"] == "shim-cli"


def test_the_listing_does_not_promise_masking_in_vs_code() -> None:
    """No VS Code hook output replaces a prompt, a tool input or a result.

    The word may appear, but only to deny it: "never masked" is the fact,
    "masks secrets" would be the false promise.
    """
    description = _manifest()["description"].lower()

    for sentence in description.split("."):
        if "mask" in sentence:
            assert "never masked" in sentence, sentence.strip()
    assert "report" in description
    assert len(_manifest()["description"]) <= 500


def test_the_hook_file_registers_what_the_adapter_supports() -> None:
    from shim_cli.clients.vscode.tool_events import INSTALLED_EVENTS

    document = json.loads(HOOKS.read_text(encoding="utf-8"))

    assert set(document["hooks"]) == {*INSTALLED_EVENTS, "UserPromptSubmit", "Stop"}


def _commands() -> list[str]:
    document = json.loads(HOOKS.read_text(encoding="utf-8"))
    return [entry["command"] for event in document["hooks"].values() for entry in event]


def test_every_event_runs_the_same_launcher_as_the_vs_code_client() -> None:
    for command in _commands():
        assert '"${PLUGIN_ROOT}/hooks/run-shim" vscode' in command


def test_the_command_stands_down_where_the_cli_installs_its_own_hook() -> None:
    """Copilot CLI and the Copilot app read this same file.

    Copilot CLI sets COPILOT_CLI on every hook process, and `shim install
    copilot` already covers that client, so running here too would inspect
    every prompt twice.
    """
    for command in _commands():
        assert '[ -n "${COPILOT_CLI:-}" ] && exit 0' in command


@pytest.mark.parametrize("event", ("UserPromptSubmit", "PreToolUse"))
def test_the_command_is_silent_under_copilot_cli(event: str, tmp_path) -> None:
    command = json.loads(HOOKS.read_text(encoding="utf-8"))["hooks"][event][0][
        "command"
    ]

    result = subprocess.run(
        ("/bin/sh", "-c", command),
        input=PROMPT,
        capture_output=True,
        env={
            "COPILOT_CLI": "1",
            "PLUGIN_ROOT": str(PLUGIN_ROOT),
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(tmp_path),
        },
        check=False,
        timeout=120,
    )

    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr == b""


def test_the_command_inspects_the_prompt_in_vs_code(tmp_path) -> None:
    command = json.loads(HOOKS.read_text(encoding="utf-8"))["hooks"][
        "UserPromptSubmit"
    ][0]["command"]
    environment = {
        key: value for key, value in os.environ.items() if key != "COPILOT_CLI"
    }

    result = subprocess.run(
        ("/bin/sh", "-c", command),
        input=PROMPT,
        capture_output=True,
        env=environment
        | {
            "PLUGIN_ROOT": str(PLUGIN_ROOT),
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
        },
        check=False,
        timeout=120,
    )

    assert result.returncode == 0
    assert b"SECRET" in result.stdout
