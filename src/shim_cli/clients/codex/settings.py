from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

try:  # pragma: no cover
    import tomllib  # ty: ignore[unresolved-import]
except ModuleNotFoundError:
    import tomli as tomllib

from shim_cli.clients.hook_settings import (
    MAX_SETTINGS_BYTES,
    Registration,
    add_groups,
    remove_groups,
)
from shim_cli.settings_files import StateKind, inspect_file

TESTED_CODEX_VERSION = "0.149.0"
MINIMUM_CODEX_VERSION = "0.149.0"
HOOK_TIMEOUT_SECONDS = 30
MAX_CONFIG_BYTES = MAX_SETTINGS_BYTES
PROMPT_EVENT = "UserPromptSubmit"


def _codex_home(home: Path | None = None) -> Path:
    try:
        if home is not None:
            return Path(home) / ".codex"
        if configured := os.environ.get("CODEX_HOME"):
            return Path(configured).expanduser()
        return Path.home() / ".codex"
    except RuntimeError as error:
        raise ValueError("Codex home path is invalid") from error


HOOK_MODULE = "shim_cli.hook"
LEGACY_HOOK_MODULE = "shim_guard.hook"


def target_path(home: Path | None = None) -> Path:
    return _codex_home(home) / "hooks.json"


def config_path(home: Path | None = None) -> Path:
    return _codex_home(home) / "config.toml"


def has_inline_hooks(path: Path | None = None) -> bool:
    target = config_path() if path is None else Path(path)
    state = inspect_file(target, MAX_CONFIG_BYTES)
    if state.kind is StateKind.ABSENT:
        return False
    if state.kind is not StateKind.FILE:
        raise ValueError("Codex config cannot be inspected safely")
    assert state.content is not None
    try:
        config = tomllib.loads(state.content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError) as error:
        raise ValueError("Codex config cannot be inspected safely") from error
    return "hooks" in config


def hook_command(
    interpreter: str | Path = sys.executable, module: str = HOOK_MODULE
) -> str:
    executable = Path(interpreter)
    if not executable.is_absolute() or not str(executable).isprintable():
        raise ValueError("hook interpreter must be an absolute safe path")
    return shlex.join((str(executable), "-I", "-B", "-m", module))


def hook_group(
    interpreter: str | Path = sys.executable, module: str = HOOK_MODULE
) -> dict[str, object]:
    return {
        "hooks": [
            {
                "command": hook_command(interpreter, module),
                "timeout": HOOK_TIMEOUT_SECONDS,
                "type": "command",
            }
        ]
    }


def hook_groups(
    interpreter: str | Path = sys.executable, module: str = HOOK_MODULE
) -> tuple[Registration, ...]:
    return ((PROMPT_EVENT, hook_group(interpreter, module)),)


def legacy_hook_groups(
    interpreter: str | Path = sys.executable,
) -> tuple[Registration, ...]:
    return hook_groups(interpreter, LEGACY_HOOK_MODULE)


def add_hook(content: bytes | None, interpreter: str | Path = sys.executable) -> bytes:
    if content is not None:
        content = remove_groups(content, legacy_hook_groups(interpreter))
    return add_groups(content, hook_groups(interpreter))


def remove_hook(content: bytes, interpreter: str | Path = sys.executable) -> bytes:
    content = remove_groups(content, legacy_hook_groups(interpreter))
    return remove_groups(content, hook_groups(interpreter))
