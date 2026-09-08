from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from shim_cli.clients.copilot.settings import (
    HOOK_TIMEOUT_SECONDS,
    MINIMUM_COPILOT_VERSION,
    TESTED_COPILOT_VERSION,
    add_hook,
    hook_command,
    hook_document,
    remove_hook,
    target_path,
)


def test_copilot_1080_hook_file_is_exact(tmp_path: Path) -> None:
    interpreter = tmp_path / "venv's python"
    content = add_hook(None, interpreter)

    assert (
        json.loads(content)
        == hook_document(interpreter)
        == {
            "version": 1,
            "hooks": {
                "userPromptTransformed": [
                    {
                        "type": "command",
                        "command": hook_command(interpreter),
                        "timeoutSec": 30,
                    }
                ]
            },
        }
    )
    assert shlex.split(hook_command(interpreter)) == [
        str(interpreter),
        "-I",
        "-B",
        "-m",
        "shim_cli.hook",
        "copilot",
    ]
    assert HOOK_TIMEOUT_SECONDS == 30
    assert TESTED_COPILOT_VERSION == MINIMUM_COPILOT_VERSION == "1.0.80"


def test_copilot_target_respects_home_and_copilot_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "configured"))

    assert target_path() == tmp_path / "configured" / "hooks" / "shim-guard.json"
    assert target_path(tmp_path) == (
        tmp_path / ".copilot" / "hooks" / "shim-guard.json"
    )


def test_copilot_hook_file_install_and_revert_are_exact(tmp_path: Path) -> None:
    interpreter = tmp_path / "python"
    installed = add_hook(None, interpreter)
    empty = remove_hook(installed, interpreter)

    assert add_hook(installed, interpreter) is installed
    assert json.loads(empty) == {"version": 1, "hooks": {}}
    assert remove_hook(empty, interpreter) is empty
    assert add_hook(empty, interpreter) == installed

    with pytest.raises(ValueError, match="unexpected content"):
        add_hook(b"{}", interpreter)
    with pytest.raises(ValueError, match="unexpected content"):
        remove_hook(b"not json", interpreter)


def test_copilot_hook_command_requires_absolute_interpreter() -> None:
    with pytest.raises(ValueError):
        hook_command("python3")
    with pytest.raises(ValueError):
        hook_command("/tmp/unsafe\x1b-python")
