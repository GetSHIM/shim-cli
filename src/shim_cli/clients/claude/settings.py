from __future__ import annotations

import os
import sys
from pathlib import Path

from shim_cli.clients.claude.tool_events import INSTALLED_EVENTS
from shim_cli.clients.hook_settings import (
    MAX_SETTINGS_BYTES,
    Registration,
    add_groups,
    remove_groups,
)
from shim_cli.session import SESSION_EVENTS

TESTED_CLAUDE_VERSION = "2.1.251"
MINIMUM_CLAUDE_VERSION = "2.1.210"
HOOK_TIMEOUT_SECONDS = 30
MAX_CONFIG_BYTES = MAX_SETTINGS_BYTES
PROMPT_EVENT = "UserPromptSubmit"
TOOL_MATCHER = "*"


def _claude_home(home: Path | None = None) -> Path:
    try:
        if home is not None:
            return Path(home) / ".claude"
        if configured := os.environ.get("CLAUDE_CONFIG_DIR"):
            return Path(configured).expanduser()
        return Path.home() / ".claude"
    except RuntimeError as error:
        raise ValueError("Claude Code home path is invalid") from error


def target_path(home: Path | None = None) -> Path:
    return _claude_home(home) / "settings.json"


def _handler(interpreter: str | Path) -> dict[str, object]:
    executable = Path(interpreter)
    if not executable.is_absolute() or not str(executable).isprintable():
        raise ValueError("hook interpreter must be an absolute safe path")
    return {
        "args": ["-I", "-B", "-m", "shim_cli.hook", "claude"],
        "command": str(executable),
        "timeout": HOOK_TIMEOUT_SECONDS,
        "type": "command",
    }


def hook_group(interpreter: str | Path = sys.executable) -> dict[str, object]:
    return {"hooks": [_handler(interpreter)]}


def tool_hook_group(interpreter: str | Path = sys.executable) -> dict[str, object]:
    return {"matcher": TOOL_MATCHER, "hooks": [_handler(interpreter)]}


def hook_groups(interpreter: str | Path = sys.executable) -> tuple[Registration, ...]:
    groups: list[Registration] = [(PROMPT_EVENT, hook_group(interpreter))]
    groups.extend((event, tool_hook_group(interpreter)) for event in INSTALLED_EVENTS)
    groups.extend((event, hook_group(interpreter)) for event in SESSION_EVENTS)
    return tuple(groups)


def add_hook(content: bytes | None, interpreter: str | Path = sys.executable) -> bytes:
    return add_groups(content, hook_groups(interpreter))


def remove_hook(content: bytes, interpreter: str | Path = sys.executable) -> bytes:
    return remove_groups(content, hook_groups(interpreter))
