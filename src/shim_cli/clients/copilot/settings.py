from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from shim_cli.clients.hook_settings import (
    HOOK_MODULE,
    MAX_SETTINGS_BYTES,
    interpreter_path,
    shim_hook,
)

TESTED_COPILOT_VERSION = "1.0.83"
MINIMUM_COPILOT_VERSION = "1.0.80"
HOOK_TIMEOUT_SECONDS = 30
MAX_CONFIG_BYTES = MAX_SETTINGS_BYTES


def _copilot_home(home: Path | None = None) -> Path:
    try:
        if home is not None:
            return Path(home) / ".copilot"
        if configured := os.environ.get("COPILOT_HOME"):
            return Path(configured).expanduser()
        return Path.home() / ".copilot"
    except RuntimeError as error:
        raise ValueError("GitHub Copilot CLI home path is invalid") from error


def target_path(home: Path | None = None) -> Path:
    return _copilot_home(home) / "hooks" / "shim.json"


def legacy_target_path(home: Path | None = None) -> Path:
    return _copilot_home(home) / "hooks" / "shim-guard.json"


def hook_command(
    interpreter: str | Path = sys.executable, module: str = HOOK_MODULE
) -> str:
    executable = interpreter_path(interpreter)
    return shlex.join((str(executable), "-I", "-B", "-m", module, "copilot"))


def hook_document(
    interpreter: str | Path = sys.executable, module: str = HOOK_MODULE
) -> dict[str, object]:
    return {
        "version": 1,
        "hooks": {
            "userPromptTransformed": [
                {
                    "type": "command",
                    "command": hook_command(interpreter, module),
                    "timeoutSec": HOOK_TIMEOUT_SECONDS,
                }
            ]
        },
    }


def _dump(document: dict[str, object]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()


_UNEXPECTED = "shim's Copilot hook file contains unexpected content"
_EMPTY = _dump({"version": 1, "hooks": {}})


def shim_hooks(content: bytes) -> list[tuple[str, str, str]] | None:
    """(event, interpreter, module) for each hook in a file shim wrote, or None.

    shim writes this file whole, so one in shim's own layout holding nothing
    but shim's hooks is shim's, whichever installation wrote it. Anything else
    is someone's own work and is never overwritten.
    """
    try:
        document = json.loads(content)
        written = _dump(document) == content
    except (ValueError, RecursionError):
        return None
    hooks = document.get("hooks") if isinstance(document, dict) else None
    if not (
        written
        and isinstance(hooks, dict)
        and set(document) == {"version", "hooks"}
        and document["version"] == 1
    ):
        return None
    found = []
    for event, handlers in hooks.items():
        if not isinstance(handlers, list):
            return None
        for handler in handlers:
            hook = shim_hook(handler)
            if hook is None:
                return None
            found.append((event, *hook))
    return found


def is_ours(content: bytes) -> bool:
    """True for a file shim wrote, from any installation, in either module spelling."""
    return shim_hooks(content) is not None


def add_hook(content: bytes | None, interpreter: str | Path = sys.executable) -> bytes:
    expected = _dump(hook_document(interpreter))
    if content == expected:
        return content
    if content is not None and not is_ours(content):
        raise ValueError(_UNEXPECTED)
    return expected


def remove_hook(content: bytes) -> bytes:
    if content == _EMPTY:
        return content
    if not is_ours(content):
        raise ValueError(_UNEXPECTED)
    return _EMPTY
