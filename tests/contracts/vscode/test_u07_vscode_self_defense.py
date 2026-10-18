"""VS Code's agent cannot switch shim off through its own tools.

VS Code refuses a call when the hook denies it, so a terminal command running
`shim config`, `shim revert` or `shim uninstall`, and an edit landing on shim's
settings or on a client settings file holding shim's hooks, are refused in
every mode. Fresh home and temporary directory per run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from shim_cli.clients.claude import settings as claude_settings

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "probe" / "vscode"
COMMAND = (sys.executable, "-I", "-B", "-m", "shim_cli.hook", "vscode")
SETTINGS_REFUSAL = (
    "shim: refused this change to {name}, which holds shim's settings or hooks; "
    "an agent that can change it can turn shim off. To change it, edit it "
    "yourself in your own terminal."
)
COMMAND_REFUSAL = (
    "shim: refused `shim {subcommand}`; an agent that can run it can turn shim "
    "off. To run it, run it yourself in your own terminal."
)


def _hook(
    tmp_path: Path,
    tool: str,
    tool_input: dict,
    settings: str = "",
    env: dict | None = None,
):
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    temporary.mkdir(exist_ok=True)
    config = home / ".config" / "shim" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.write_text(settings, encoding="utf-8")
    config.chmod(0o600)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SHIM_", "XDG_", "CLAUDE_", "CODEX_", "COPILOT_"))
    }
    environment.update(HOME=str(home), TMPDIR=str(temporary), **(env or {}))
    # The captured call, with only the tool, its input and the folder changed.
    document = json.loads((FIXTURES / "PreToolUse-run_in_terminal-1.json").read_text())
    document.update(tool_name=tool, tool_input=tool_input, cwd=str(home))
    result = subprocess.run(
        COMMAND,
        input=json.dumps(document).encode(),
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
    return (json.loads(result.stdout) if result.stdout else None), records, config


def _denied(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


@pytest.mark.parametrize("settings", ("", '[mode]\ndefault = "observe"\n'))
@pytest.mark.parametrize(
    ("tool", "tool_input", "subcommand"),
    (
        (
            "run_in_terminal",
            {"command": "shim config --only email", "mode": "sync"},
            "config",
        ),
        ("send_to_terminal", {"command": "FOO=1 shim revert claude"}, "revert"),
        (
            "run_in_terminal",
            {"command": "shim -- uninstall", "mode": "sync"},
            "uninstall",
        ),
        (
            "create_and_run_task",
            {
                "workspaceFolder": "/probe/workspace",
                "task": {
                    "label": "tidy",
                    "type": "shell",
                    "command": "shim",
                    "args": ["config", "--reset", "--yes"],
                },
            },
            "config",
        ),
    ),
)
def test_a_terminal_command_running_shim_s_controls_is_refused(
    tmp_path: Path, tool: str, tool_input: dict, subcommand: str, settings: str
) -> None:
    (tmp_path / "home").mkdir()

    output, [record], _config = _hook(tmp_path, tool, tool_input, settings)

    assert output == _denied(COMMAND_REFUSAL.format(subcommand=subcommand))
    assert (record["client"], record["action"], record["note"]) == (
        "vscode",
        "deny",
        "self-protection",
    )
    assert record["mode"] == ("observe" if settings else "warn")


def test_a_terminal_command_that_only_mentions_them_runs(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()

    output, [record], _config = _hook(
        tmp_path, "run_in_terminal", {"command": 'grep -n "shim config" README.md'}
    )

    assert output is None
    assert record["action"] == "allow"


def _edits(path: Path) -> dict:
    patch = f"*** Begin Patch\n*** Delete File: {path}\n*** End Patch\n"
    return dict(
        (
            (
                "create_file",
                {"filePath": str(path), "content": "enabled_entities = []\n"},
            ),
            (
                "replace_string_in_file",
                {"filePath": str(path), "oldString": "a", "newString": "b"},
            ),
            (
                "insert_edit_into_file",
                {"filePath": str(path), "code": "x", "explanation": "e"},
            ),
            (
                "multi_replace_string_in_file",
                {
                    "explanation": "e",
                    "replacements": [
                        {
                            "filePath": "/probe/workspace/a.md",
                            "oldString": "a",
                            "newString": "b",
                        },
                        {"filePath": str(path), "oldString": "a", "newString": "b"},
                    ],
                },
            ),
            ("apply_patch", {"input": patch, "explanation": "e"}),
        )
    )


@pytest.mark.parametrize(
    "tool",
    (
        "apply_patch",
        "create_file",
        "insert_edit_into_file",
        "multi_replace_string_in_file",
        "replace_string_in_file",
    ),
)
def test_an_edit_of_shim_s_settings_is_refused(tmp_path: Path, tool: str) -> None:
    home = tmp_path / "home"
    tool_input = _edits(home / ".config" / "shim" / "config.toml")[tool]

    output, [record], config = _hook(tmp_path, tool, tool_input)

    assert output == _denied(SETTINGS_REFUSAL.format(name="config.toml"))
    assert (record["action"], record["note"]) == ("deny", "self-protection")
    assert config.read_text(encoding="utf-8") == ""


def test_an_edit_of_claude_settings_holding_shim_s_hooks_is_refused(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(claude_settings.add_hook(None, sys.executable))
    patch = (
        "*** Begin Patch\n"
        "*** Update File: /probe/workspace/notes.md\n"
        f"*** Move to: {settings}\n"
        "*** End Patch\n"
    )

    output, [record], _config = _hook(
        tmp_path, "apply_patch", {"input": patch, "explanation": "e"}
    )

    assert output == _denied(SETTINGS_REFUSAL.format(name="settings.json"))
    assert record["action"] == "deny"


def test_a_patch_that_only_mentions_the_settings_in_its_text_is_not_refused(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    settings = home / ".config" / "shim" / "config.toml"
    patch = (
        "*** Begin Patch\n"
        "*** Update File: /probe/workspace/notes.md\n"
        "@@\n"
        f"+*** Update File: {settings}\n"
        "*** End Patch\n"
    )

    output, [record], _config = _hook(
        tmp_path, "apply_patch", {"input": patch, "explanation": "e"}
    )

    assert output is None
    assert record["action"] == "allow"


def _task(task: dict) -> dict:
    return {"workspaceFolder": "/probe/workspace", "task": {"label": "t", **task}}


@pytest.mark.parametrize(
    ("task", "subcommand"),
    (
        # tasks.json's quoted form of an argument.
        (
            {
                "type": "shell",
                "command": "shim",
                "args": [{"value": "config", "quoting": "escape"}, "--only", "EMAIL"],
            },
            "config",
        ),
        ({"type": "shell", "command": {"value": "shim"}, "args": ["revert"]}, "revert"),
        # VS Code quotes a shell task's argument only when it holds a space
        # outside quotes, and then wraps it in single quotes as it stands.
        ({"type": "shell", "command": "echo", "args": ["$(shim", "config)"]}, "config"),
        (
            {
                "type": "shell",
                "command": "echo",
                "args": ['a "', ";", "shim", "config"],
            },
            "config",
        ),
        # An argument already wrapped in quotes is written as it stands.
        (
            {"type": "shell", "command": "echo", "args": ['"x" ; shim config "y"']},
            "config",
        ),
        (
            {
                "type": "shell",
                "command": "echo",
                "args": [{"value": "$(shim revert claude)", "quoting": "weak"}],
            },
            "revert",
        ),
        # On macOS VS Code runs the task's `osx` command instead.
        (
            {
                "type": "shell",
                "command": "echo ok",
                "osx": {"command": "shim", "args": ["uninstall"]},
            },
            "uninstall",
        ),
    ),
)
def test_a_task_running_shim_s_controls_in_any_form_is_refused(
    tmp_path: Path, task: dict, subcommand: str
) -> None:
    (tmp_path / "home").mkdir()

    output, [record], _config = _hook(tmp_path, "create_and_run_task", _task(task))

    assert output == _denied(COMMAND_REFUSAL.format(subcommand=subcommand))
    assert (record["action"], record["note"]) == ("deny", "self-protection")


@pytest.mark.parametrize(
    "task",
    (
        {"type": "shell", "command": "grep", "args": ["-n", "shim config", "a.md"]},
        # Single quotes are the default quoting: the text is data.
        {"type": "shell", "command": "echo", "args": [{"value": "$(shim config)"}]},
        {
            "type": "shell",
            "command": "echo",
            "args": [{"value": "$(shim config)", "quoting": "strong"}],
        },
        {
            "type": "shell",
            "command": "echo",
            "args": [{"value": "x; shim revert claude", "quoting": "escape"}],
        },
    ),
)
def test_a_task_that_only_mentions_them_runs(tmp_path: Path, task: dict) -> None:
    (tmp_path / "home").mkdir()

    output, [record], _config = _hook(tmp_path, "create_and_run_task", _task(task))

    assert output is None
    assert record["action"] == "allow"


def _uris(path: Path) -> dict:
    """The strings VS Code's edit tools resolve to `path` (Copilot Chat 0.69.0
    resolveFilePath): a URI, or any string not starting with `/` that holds a
    `scheme:`, read from the root."""
    text = str(path)
    return {
        "file URI": f"file://{text}",
        "percent-encoded": f"file://{text.replace('shim', 's%68im')}",
        # `file://var/p` writes `//var/p`. macOS's /private/var is also /var:
        # spelled from /var, the path after the authority names no file.
        "authority": f"file:/{text.removeprefix('/private')}",
        "user data": f"vscode-userdata:{text}",
        "colon in the query": text.lstrip("/") + "?v=a:b",
    }


@pytest.mark.parametrize(
    "spelled",
    ("file URI", "percent-encoded", "authority", "user data", "colon in the query"),
)
def test_an_edit_naming_shim_s_settings_by_uri_is_refused(
    tmp_path: Path, spelled: str
) -> None:
    home = tmp_path / "home"
    path = _uris(home / ".config" / "shim" / "config.toml")[spelled]

    output, [record], config = _hook(
        tmp_path, "create_file", {"filePath": path, "content": "enabled_entities = []"}
    )

    assert output == _denied(SETTINGS_REFUSAL.format(name="config.toml"))
    assert (record["action"], record["note"]) == ("deny", "self-protection")
    assert config.read_text(encoding="utf-8") == ""


def test_a_patch_naming_shim_s_settings_by_uri_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    uri = _uris(home / ".config" / "shim" / "config.toml")["file URI"]
    patch = f"*** Begin Patch\n*** Update File: {uri}\n@@\n+x\n*** End Patch\n"

    output, [record], _config = _hook(
        tmp_path, "apply_patch", {"input": patch, "explanation": "e"}
    )

    assert output == _denied(SETTINGS_REFUSAL.format(name="config.toml"))
    assert record["action"] == "deny"


def test_an_edit_of_another_file_by_uri_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()

    output, [record], _config = _hook(
        tmp_path,
        "create_file",
        {"filePath": "file:///probe/workspace/notes.md", "content": "hello"},
    )

    assert output is None
    assert record["action"] == "allow"


@pytest.mark.parametrize(
    "text",
    (
        "file:///Users/me/.config/shim/config.toml",
        "vscode-userdata:/Users/me/settings.json",
        "Users/me/.config/shim/config.toml?v=a:b",
        "notes/x:y",
        "a+.-:x",
        "é:x",
        "+:x",
        "a: b",
        "a:",
        "/work/x:y",
        "notes.md",
    ),
)
def test_a_path_is_read_as_a_uri_exactly_when_vs_code_reads_one(text: str) -> None:
    """Copilot Chat 0.69.0 searches the whole string for a `scheme:`, not only
    its start, so a colon in a query still makes a URI of a relative path."""
    from shim_cli import hook

    as_uri = not text.startswith("/") and re.search(r"\w[\w\d+.-]*:\S", text)

    assert (len(hook._paths(text)) > 1) == bool(as_uri)


def test_a_megabyte_long_path_is_read_in_linear_time(tmp_path: Path) -> None:
    """The alarm cannot stop a regular expression, and a client that times the
    hook out runs the call. Searched from every start, 16,000 characters took
    0.9 s and a megabyte about an hour; read once, it is a fraction of a second."""
    code = "from shim_cli.hook import _paths; _paths('a' * 1_000_000)"
    environment = dict(os.environ, HOME=str(tmp_path), TMPDIR=str(tmp_path))

    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", code),
        capture_output=True,
        cwd=ROOT,
        env=environment,
        check=False,
        timeout=30,
    )

    assert (result.returncode, result.stderr) == (0, b"")


@pytest.mark.parametrize(
    "inside", ("hooks/run-shim", "com.github.copilot/hooks/hooks.json", "bin/shim.pyz")
)
def test_an_edit_inside_the_plugin_folder_running_the_hook_is_refused(
    tmp_path: Path, inside: str
) -> None:
    # VS Code names the plugin folder in PLUGIN_ROOT; its files are shim's hooks.
    (tmp_path / "home").mkdir()
    plugin = tmp_path / "shim-cli" / "plugins" / "shim-cli"
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "hooks" / "run-shim").write_text("#!/bin/sh\n", encoding="utf-8")
    target = plugin / inside

    output, [record], _config = _hook(
        tmp_path,
        "create_file",
        {"filePath": str(target), "content": "exit 0"},
        env={"PLUGIN_ROOT": str(plugin)},
    )

    assert output == _denied(SETTINGS_REFUSAL.format(name=target.name))
    assert (record["action"], record["note"]) == ("deny", "self-protection")
    assert (plugin / "hooks" / "run-shim").read_text(encoding="utf-8") == "#!/bin/sh\n"
