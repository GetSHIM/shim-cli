from __future__ import annotations

import json
from pathlib import Path

import pytest

from shim_cli.clients.claude.settings import (
    HOOK_TIMEOUT_SECONDS,
    MINIMUM_CLAUDE_VERSION,
    TESTED_CLAUDE_VERSION,
    add_hook,
    hook_group,
    hook_groups,
    remove_hook,
    target_path,
    tool_hook_group,
)
from shim_cli.clients.claude.tool_events import INSTALLED_EVENTS
from shim_cli.session import SESSION_EVENTS


def test_claude_code_settings_use_shell_free_exec_form(tmp_path: Path) -> None:
    interpreter = tmp_path / "venv's python"
    handler = {
        "args": ["-I", "-B", "-m", "shim_cli.hook", "claude"],
        "command": str(interpreter),
        "timeout": 30,
        "type": "command",
    }

    assert hook_group(interpreter) == {"hooks": [handler]}
    assert tool_hook_group(interpreter) == {"matcher": "*", "hooks": [handler]}
    assert HOOK_TIMEOUT_SECONDS == 30
    assert MINIMUM_CLAUDE_VERSION == "2.1.210"
    assert TESTED_CLAUDE_VERSION == "2.1.251"


def test_claude_code_registers_the_prompt_tool_and_session_events(
    tmp_path: Path,
) -> None:
    interpreter = tmp_path / "python"
    expected_tool_events = list(INSTALLED_EVENTS)

    events = [event for event, _group in hook_groups(interpreter)]
    assert events == ["UserPromptSubmit", *expected_tool_events, *SESSION_EVENTS]
    assert len(set(events)) == len(events)

    document = json.loads(add_hook(None, interpreter))
    assert document == {
        "hooks": {
            "UserPromptSubmit": [hook_group(interpreter)],
            **{event: [tool_hook_group(interpreter)] for event in expected_tool_events},
            **{event: [hook_group(interpreter)] for event in SESSION_EVENTS},
        }
    }


def test_claude_code_target_respects_config_dir_and_injected_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom-claude"))
    assert target_path() == tmp_path / "custom-claude" / "settings.json"
    assert target_path(tmp_path) == tmp_path / ".claude" / "settings.json"


def test_claude_code_settings_preserve_unrelated_values_and_hooks(
    tmp_path: Path,
) -> None:
    interpreter = tmp_path / "python"
    existing_group = {"hooks": [{"type": "command", "command": "existing"}]}
    original = {
        "permissions": {"allow": ["Read"]},
        "hooks": {
            "SessionStart": [existing_group],
            "UserPromptSubmit": [existing_group],
            "PreToolUse": [existing_group],
        },
        "model": "sonnet",
    }

    installed = add_hook(json.dumps(original).encode(), interpreter)
    document = json.loads(installed)
    assert list(document) == ["permissions", "hooks", "model"]
    assert document["permissions"] == original["permissions"]
    assert document["hooks"]["SessionStart"] == [existing_group]
    for event, group in hook_groups(interpreter):
        assert document["hooks"][event][-1] == group
        if event in original["hooks"]:
            assert document["hooks"][event][0] == existing_group
    assert add_hook(installed, interpreter) == installed
    assert json.loads(remove_hook(installed, interpreter)) == original


def test_claude_code_settings_upgrade_a_prompt_only_install_in_place(
    tmp_path: Path,
) -> None:
    interpreter = tmp_path / "python"
    prompt_only = (
        json.dumps({"hooks": {"UserPromptSubmit": [hook_group(interpreter)]}}) + "\n"
    ).encode()

    upgraded = add_hook(prompt_only, interpreter)
    document = json.loads(upgraded)
    assert document["hooks"]["UserPromptSubmit"] == [hook_group(interpreter)]
    assert len(document["hooks"]) == len(hook_groups(interpreter))
    assert json.loads(remove_hook(upgraded, interpreter)) == {}


def test_claude_code_revert_leaves_a_foreign_tool_hook_untouched(
    tmp_path: Path,
) -> None:
    interpreter = tmp_path / "python"
    foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "audit"}]}
    original = {"hooks": {"PreToolUse": [foreign]}}

    installed = add_hook(json.dumps(original).encode(), interpreter)
    assert json.loads(remove_hook(installed, interpreter)) == original


@pytest.mark.parametrize(
    "content",
    [
        b'{"hooks":{} ,"hooks":{}}',
        b'{"value":NaN}',
        b"[]",
        b'{"hooks":null}',
        b'{"hooks":{"UserPromptSubmit":{}}}',
        b'{"hooks":{"UserPromptSubmit":[{}]}}',
        b'{"hooks":{"UserPromptSubmit":[{"hooks":[{}]}]}}',
        b'{"hooks":{"PreToolUse":[{"hooks":[{"type":"command"}]}]}}',
    ],
)
def test_claude_code_settings_reject_malformed_documents(content: bytes) -> None:
    with pytest.raises(ValueError):
        add_hook(content)
