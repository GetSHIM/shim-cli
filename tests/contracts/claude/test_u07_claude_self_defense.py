"""The agent shim guards cannot switch shim off through its own tools.

Each run is the hook as Claude Code starts it, in a fresh home and temporary
directory: a write to shim's settings, or to a client settings file holding
shim's hooks, and a shell command running `shim config`, `shim revert` or
`shim uninstall`, are refused in every mode. Nothing here touches the real
user's settings.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from shim_cli.clients.claude import settings as claude_settings
from shim_cli.clients.codex import settings as codex_settings
from shim_cli.clients.copilot import settings as copilot_settings

ROOT = Path(__file__).resolve().parents[3]
COMMAND = (sys.executable, "-I", "-B", "-m", "shim_cli.hook", "claude")
WEAKER = "enabled_entities = []\n"
SETTINGS_REFUSAL = (
    "shim: refused this change to {name}, which holds shim's settings or hooks; "
    "an agent that can change it can turn shim off. To change it, edit it "
    "yourself in your own terminal."
)
COMMAND_REFUSAL = (
    "shim: refused `shim {subcommand}`; an agent that can run it can turn shim "
    "off. To run it, run it yourself in your own terminal."
)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return home


def _hook(
    tmp_path: Path,
    tool: str,
    tool_input: dict,
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    document: dict | None = None,
):
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    temporary.mkdir(exist_ok=True)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SHIM_", "XDG_", "CLAUDE_", "CODEX_", "COPILOT_"))
    }
    environment.update(HOME=str(home), TMPDIR=str(temporary), **(env or {}))
    event = document or {
        "session_id": "u07",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": str(cwd or home),
        "permission_mode": "bypassPermissions",
        "hook_event_name": "PreToolUse",
    }
    event = {**event, "tool_name": tool, "tool_input": tool_input}
    result = subprocess.run(
        COMMAND,
        input=json.dumps(event).encode(),
        capture_output=True,
        cwd=ROOT,
        env=environment,
        check=False,
        timeout=60,
    )
    assert (result.returncode, result.stderr) == (0, b"")
    records = [
        json.loads(line)
        for path in temporary.rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    for path in temporary.rglob("*.jsonl"):
        path.unlink()
    return (json.loads(result.stdout) if result.stdout else None), records


def _denied(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _assert_refused(output, records, reason: str, direction: str) -> None:
    assert output == _denied(reason)
    [record] = records
    assert (record["direction"], record["action"], record["note"]) == (
        direction,
        "deny",
        "self-protection",
    )
    assert record["entities"] == {}


def _write_shim_settings(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


@pytest.mark.parametrize(
    ("location", "env"),
    (
        (".config/shim/config.toml", {}),
        (".config/shim-guard/config.toml", {}),
        ("xdg/shim/config.toml", {"XDG_CONFIG_HOME": "{home}/xdg"}),
        ("pinned/settings.toml", {"SHIM_CONFIG": "{home}/pinned/settings.toml"}),
    ),
)
@pytest.mark.parametrize("tool", ("Write", "Edit", "MultiEdit"))
def test_a_write_to_shim_s_own_settings_is_refused(
    tmp_path: Path, location: str, env: dict, tool: str
) -> None:
    home = _home(tmp_path)
    env = {key: value.format(home=home) for key, value in env.items()}
    target = home / location
    tool_input = {
        "Write": {"file_path": str(target), "content": WEAKER},
        "Edit": {
            "file_path": str(target),
            "old_string": "enabled_entities",
            "new_string": "# enabled_entities",
        },
        "MultiEdit": {
            "file_path": str(target),
            "edits": [{"old_string": "a", "new_string": "b"}],
        },
    }[tool]

    output, records = _hook(tmp_path, tool, tool_input, env=env)

    _assert_refused(
        output, records, SETTINGS_REFUSAL.format(name=target.name), "local-write"
    )
    # The scrubbed target, cut to its last 120 characters as every record is.
    assert records[0]["target"].endswith(location)
    assert str(target).endswith(records[0]["target"].removeprefix("…"))
    assert WEAKER.strip() not in json.dumps(records)


def test_the_settings_file_is_refused_by_a_relative_path(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _write_shim_settings(home / ".config" / "shim" / "config.toml")

    output, records = _hook(
        tmp_path,
        "Write",
        {"file_path": "shim/../shim/config.toml", "content": WEAKER},
        cwd=home / ".config",
    )

    _assert_refused(
        output, records, SETTINGS_REFUSAL.format(name="config.toml"), "local-write"
    )


@pytest.mark.parametrize("exists", (True, False))
def test_the_settings_file_is_refused_through_a_symlinked_directory(
    tmp_path: Path, exists: bool
) -> None:
    home = _home(tmp_path)
    (home / ".config" / "shim").mkdir(parents=True, mode=0o700)
    if exists:
        _write_shim_settings(home / ".config" / "shim" / "config.toml")
    link = tmp_path / "elsewhere"
    link.symlink_to(home / ".config" / "shim", target_is_directory=True)

    output, records = _hook(
        tmp_path, "Write", {"file_path": str(link / "config.toml"), "content": WEAKER}
    )

    _assert_refused(
        output, records, SETTINGS_REFUSAL.format(name="config.toml"), "local-write"
    )


@pytest.mark.parametrize("spelled", ("absolute", "from home"))
def test_a_settings_file_that_does_not_exist_yet_is_still_refused(
    tmp_path: Path, spelled: str
) -> None:
    home = _home(tmp_path)
    target = home / ".config" / "shim" / "config.toml"
    given = str(target) if spelled == "absolute" else "~/.config/shim/config.toml"

    output, records = _hook(tmp_path, "Write", {"file_path": given, "content": WEAKER})

    _assert_refused(
        output, records, SETTINGS_REFUSAL.format(name="config.toml"), "local-write"
    )
    assert not target.exists()


@pytest.mark.parametrize(
    "location", (".config/Shim/config.toml", ".CONFIG/shim/Config.TOML")
)
def test_a_differently_cased_settings_path_is_refused_before_the_file_exists(
    tmp_path: Path, location: str
) -> None:
    # On macOS's default disk this spelling creates shim's settings.
    home = _home(tmp_path)

    output, records = _hook(
        tmp_path, "Write", {"file_path": str(home / location), "content": WEAKER}
    )

    _assert_refused(
        output, records, SETTINGS_REFUSAL.format(name="config.toml"), "local-write"
    )
    assert not (home / ".config").exists()


def test_a_settings_path_spelled_in_another_unicode_form_is_refused(
    tmp_path: Path,
) -> None:
    _home(tmp_path)
    composed = tmp_path / unicodedata.normalize("NFC", "caf\u00e9") / "shim.toml"
    decomposed = unicodedata.normalize("NFD", str(composed))
    assert decomposed != str(composed)

    output, records = _hook(
        tmp_path,
        "Write",
        {"file_path": decomposed, "content": WEAKER},
        env={"SHIM_CONFIG": str(composed)},
    )

    _assert_refused(
        output, records, SETTINGS_REFUSAL.format(name="shim.toml"), "local-write"
    )


def _client_files(home: Path) -> dict:
    """Each client file, with content holding shim's hooks and without."""
    hooks = copilot_settings.add_hook(None, sys.executable)
    return {
        "claude": (
            home / ".claude" / "settings.json",
            claude_settings.add_hook(b'{"theme": "dark"}\n', sys.executable),
            b'{"theme": "dark"}\n',
        ),
        "codex": (
            home / ".codex" / "hooks.json",
            codex_settings.add_hook(None, sys.executable),
            b"{}\n",
        ),
        # What `shim revert copilot` leaves behind.
        "copilot": (
            home / ".copilot" / "hooks" / "shim.json",
            hooks,
            copilot_settings.remove_hook(hooks),
        ),
    }


def _place(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.mark.parametrize("client", ("claude", "codex", "copilot"))
def test_a_client_settings_file_holding_shim_s_hooks_is_refused(
    tmp_path: Path, client: str
) -> None:
    home = _home(tmp_path)
    path, holding, _without = _client_files(home)[client]
    _place(path, holding)

    output, records = _hook(
        tmp_path,
        "Edit",
        {"file_path": str(path), "old_string": "shim_cli", "new_string": "x"},
    )

    _assert_refused(
        output, records, SETTINGS_REFUSAL.format(name=path.name), "local-write"
    )
    assert path.read_bytes() == holding


@pytest.mark.parametrize("client", ("claude", "codex", "copilot"))
def test_a_client_settings_file_without_shim_s_hooks_is_left_to_the_agent(
    tmp_path: Path, client: str
) -> None:
    home = _home(tmp_path)
    path, _holding, without = _client_files(home)[client]
    _place(path, without)

    output, [record] = _hook(
        tmp_path,
        "Write",
        {"file_path": str(path), "content": '{"theme": "light"}\n'},
    )

    assert output is None
    assert (record["action"], record["note"]) == ("allow", "")


def test_codex_settings_are_refused_while_its_hook_file_holds_shim(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    path, holding, _without = _client_files(home)["codex"]
    _place(path, holding)
    settings = home / ".codex" / "config.toml"
    tool_input = {"file_path": str(settings), "content": "[features]\nhooks = false\n"}

    output, records = _hook(tmp_path, "Write", tool_input)

    _assert_refused(
        output, records, SETTINGS_REFUSAL.format(name="config.toml"), "local-write"
    )

    path.write_bytes(b"{}\n")
    output, [record] = _hook(tmp_path, "Write", tool_input)
    assert output is None
    assert record["action"] == "allow"


def test_a_client_settings_file_is_refused_through_a_hard_link(tmp_path: Path) -> None:
    # shim's own settings refuse a hard link when they are read, so it is a
    # client's file that another name can reach.
    home = _home(tmp_path)
    path, holding, _without = _client_files(home)["claude"]
    _place(path, holding)
    link = tmp_path / "notes.json"
    link.hardlink_to(path)

    output, records = _hook(
        tmp_path, "Write", {"file_path": str(link), "content": "{}"}
    )

    _assert_refused(
        output, records, SETTINGS_REFUSAL.format(name="settings.json"), "local-write"
    )
    assert path.read_bytes() == holding


def test_a_claude_settings_file_found_through_claude_config_dir_is_refused(
    tmp_path: Path,
) -> None:
    _home(tmp_path)
    folder = tmp_path / "claude-config"
    path = folder / "settings.json"
    _place(path, claude_settings.add_hook(None, sys.executable))

    output, records = _hook(
        tmp_path,
        "Write",
        {"file_path": str(path), "content": "{}\n"},
        env={"CLAUDE_CONFIG_DIR": str(folder)},
    )

    _assert_refused(
        output, records, SETTINGS_REFUSAL.format(name="settings.json"), "local-write"
    )


@pytest.mark.parametrize(
    "plugins",
    (
        {"shim-cli@shim-cli": True},
        {"other@market": True, "shim-cli@shim-cli": True},
        {"shim-guard@shim-guard": True},
    ),
)
def test_claude_settings_switching_on_shim_s_plugin_are_refused(
    tmp_path: Path, plugins: dict
) -> None:
    # The marketplace plugin holds the hooks; these settings switch it on.
    home = _home(tmp_path)
    path = home / ".claude" / "settings.json"
    content = json.dumps({"enabledPlugins": plugins}).encode()
    _place(path, content)

    output, records = _hook(
        tmp_path,
        "Edit",
        {"file_path": str(path), "old_string": "true", "new_string": "false"},
    )

    _assert_refused(
        output, records, SETTINGS_REFUSAL.format(name="settings.json"), "local-write"
    )
    assert path.read_bytes() == content


@pytest.mark.parametrize(
    "plugins", ({"other@market": True}, {"shim-cli@shim-cli": False})
)
def test_claude_settings_not_running_shim_s_plugin_are_left_to_the_agent(
    tmp_path: Path, plugins: dict
) -> None:
    home = _home(tmp_path)
    path = home / ".claude" / "settings.json"
    _place(path, json.dumps({"enabledPlugins": plugins}).encode())

    output, [record] = _hook(
        tmp_path, "Write", {"file_path": str(path), "content": "{}\n"}
    )

    assert output is None
    assert (record["action"], record["note"]) == ("allow", "")


def test_a_plugin_root_that_is_not_shim_s_plugin_is_not_protected(
    tmp_path: Path,
) -> None:
    # Only a folder holding shim's launcher is shim's plugin.
    _home(tmp_path)
    project = tmp_path / "project"
    (project / "hooks").mkdir(parents=True)

    output, [record] = _hook(
        tmp_path,
        "Write",
        {"file_path": str(project / "hooks" / "claude.json"), "content": "{}"},
        env={"CLAUDE_PLUGIN_ROOT": str(project)},
    )

    assert output is None
    assert record["action"] == "allow"


@pytest.mark.parametrize(
    ("command", "subcommand"),
    (
        ("shim config --only email", "config"),
        ("FOO=1 shim revert claude", "revert"),
        ("cd x && /usr/local/bin/shim uninstall", "uninstall"),
        ("shim config --only US_SSN --yes", "config"),
        ("shim -- config --only US_SSN --yes", "config"),
        ("SHIM revert claude", "revert"),
    ),
)
@pytest.mark.parametrize("tool", ("Bash", "Monitor", "PowerShell"))
def test_a_shell_command_running_shim_s_controls_is_refused(
    tmp_path: Path, command: str, subcommand: str, tool: str
) -> None:
    _home(tmp_path)

    output, records = _hook(tmp_path, tool, {"command": command})

    _assert_refused(
        output,
        records,
        COMMAND_REFUSAL.format(subcommand=subcommand),
        "executable-text",
    )
    assert records[0]["target"] == ""
    assert command not in json.dumps(records)


@pytest.mark.parametrize(
    "command",
    (
        "echo shim config",
        'grep "shim config" README.md',
        "shimmer config",
        "shim doctor claude",
        "git commit -F - <<'EOF'\nshim config: keep options\nEOF",
    ),
)
def test_a_shell_command_that_only_mentions_them_runs(
    tmp_path: Path, command: str
) -> None:
    _home(tmp_path)

    output, [record] = _hook(tmp_path, "Bash", {"command": command})

    assert output is None
    assert (record["action"], record["note"]) == ("allow", "")


def test_the_refusal_holds_in_observe_mode(tmp_path: Path) -> None:
    home = _home(tmp_path)
    settings = home / ".config" / "shim" / "config.toml"
    _write_shim_settings(settings, '[mode]\ndefault = "observe"\n')

    output, records = _hook(
        tmp_path, "Write", {"file_path": str(settings), "content": WEAKER}
    )
    _assert_refused(
        output, records, SETTINGS_REFUSAL.format(name="config.toml"), "local-write"
    )
    assert records[0]["mode"] == "observe"

    output, records = _hook(tmp_path, "Bash", {"command": "shim revert claude --yes"})
    _assert_refused(
        output, records, COMMAND_REFUSAL.format(subcommand="revert"), "executable-text"
    )
    assert records[0]["mode"] == "observe"
    assert settings.read_text(encoding="utf-8") == '[mode]\ndefault = "observe"\n'


def test_a_read_of_shim_s_settings_is_not_refused(tmp_path: Path) -> None:
    home = _home(tmp_path)
    settings = home / ".config" / "shim" / "config.toml"
    _write_shim_settings(settings)

    output, [record] = _hook(tmp_path, "Read", {"file_path": str(settings)})

    assert output is None
    assert record["action"] == "allow"


def test_vs_code_running_the_claude_hook_refuses_an_apply_patch_to_the_settings(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    settings = home / ".config" / "shim" / "config.toml"
    _write_shim_settings(settings)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: /work/notes.md\n"
        "@@\n-a\n+b\n"
        f"*** Update File: {settings}\n"
        "@@\n+enabled_entities = []\n"
        "*** End Patch\n"
    )
    # VS Code runs the hook installed for Claude Code, and says so by sending
    # a timestamp and no permission mode.
    document = {
        "timestamp": "2026-10-18T08:00:00.000Z",
        "hook_event_name": "PreToolUse",
        "session_id": "u07",
        "cwd": "/work",
    }

    output, records = _hook(
        tmp_path,
        "apply_patch",
        {"input": patch, "explanation": "Tidy"},
        document=document,
    )

    _assert_refused(
        output, records, SETTINGS_REFUSAL.format(name="config.toml"), "local-write"
    )
    assert records[0]["client"] == "vscode"
