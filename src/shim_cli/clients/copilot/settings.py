from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from shim_cli.clients.hook_settings import MAX_SETTINGS_BYTES, interpreter_path

TESTED_COPILOT_VERSION = "1.0.80"
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


HOOK_MODULE = "shim_cli.hook"
LEGACY_HOOK_MODULE = "shim_guard.hook"


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


def add_hook(content: bytes | None, interpreter: str | Path = sys.executable) -> bytes:
    expected = _dump(hook_document(interpreter))
    legacy = _dump(hook_document(interpreter, LEGACY_HOOK_MODULE))
    empty = _dump({"version": 1, "hooks": {}})
    if content is None or content in (empty, legacy):
        return expected
    if content == expected:
        return content
    raise ValueError("shim's Copilot hook file contains unexpected content")


def is_legacy(content: bytes, interpreter: str | Path = sys.executable) -> bool:
    return content == _dump(hook_document(interpreter, LEGACY_HOOK_MODULE))


def is_ours(content: bytes, interpreter: str | Path = sys.executable) -> bool:
    """True for a file this tool wrote, under either module spelling."""
    return content in (
        _dump(hook_document(interpreter)),
        _dump(hook_document(interpreter, LEGACY_HOOK_MODULE)),
        _dump({"version": 1, "hooks": {}}),
    )


def remove_hook(content: bytes, interpreter: str | Path = sys.executable) -> bytes:
    expected = _dump(hook_document(interpreter))
    legacy = _dump(hook_document(interpreter, LEGACY_HOOK_MODULE))
    empty = _dump({"version": 1, "hooks": {}})
    if content in (expected, legacy):
        return empty
    if content == empty:
        return content
    raise ValueError("shim's Copilot hook file contains unexpected content")
