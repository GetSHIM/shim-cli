from __future__ import annotations

import json
import math
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

MAX_SETTINGS_BYTES = 1_000_000
HOOK_MODULE = "shim_cli.hook"
LEGACY_HOOK_MODULE = "shim_guard.hook"

Registration = tuple

# One interpreter answers to several names. `sys.executable` reports whichever
# one started Python, so a console script says `.venv/bin/python3` while
# `python -m` says `.venv/bin/python`.
INTERPRETER_ALIASES = ("python", "python3", f"python3.{sys.version_info.minor}")


def interpreter_path(interpreter: str | Path) -> Path:
    """The name a hook fragment should use for this interpreter.

    A fragment is matched as an exact string, so the two names above disagree
    about a hook that is already installed: `install` adds a second one and
    `doctor` reports the first missing. Answer with the same alias whoever
    asks, without resolving the symlink, which would leave the environment.
    """
    executable = Path(interpreter)
    if not executable.is_absolute() or not str(executable).isprintable():
        raise ValueError("hook interpreter must be an absolute safe path")
    for name in INTERPRETER_ALIASES:
        try:
            candidate = executable.with_name(name)
            if candidate.samefile(executable):
                return candidate
        except (OSError, ValueError):
            continue
    return executable


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate JSON key")
    return value


def _constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def _load(content: bytes) -> dict[str, object]:
    if len(content) > MAX_SETTINGS_BYTES:
        raise ValueError("hook settings are too large")
    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_object,
            parse_constant=_constant,
            parse_float=_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("invalid hook settings") from error
    if not isinstance(document, dict):
        raise ValueError("hook settings must be an object")
    if "hooks" not in document:
        return document
    hooks = document["hooks"]
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    for groups in hooks.values():
        if not isinstance(groups, list):
            raise ValueError("hook events must be lists")
        for group in groups:
            _validate_group(group)
    return document


def _validate_group(group: object) -> None:
    if not isinstance(group, dict):
        raise ValueError("hook groups must be objects")
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        raise ValueError("hook groups require hook lists")
    for handler in handlers:
        if not isinstance(handler, dict):
            raise ValueError("hook handlers must be objects")
        hook_type = handler.get("type")
        if not isinstance(hook_type, str):
            raise ValueError("hook handler types must be strings")
        if hook_type == "command" and not (
            isinstance(handler.get("command"), str) and handler["command"]
        ):
            raise ValueError("command hooks require a command")


def _dump(document: dict[str, object]) -> bytes:
    try:
        content = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    except (UnicodeEncodeError, RecursionError) as error:
        raise ValueError("invalid hook settings") from error
    if len(content) > MAX_SETTINGS_BYTES:
        raise ValueError("hook settings are too large")
    return content


# The handler keys every release wrote for each client, and the argument that
# follows `-I -B -m <module>`: Claude Code's exec-form `command` and `args`,
# Codex's shell command, and Copilot's shell command.
_LAYOUTS = {
    frozenset({"args", "command", "timeout", "type"}): ["claude"],
    frozenset({"command", "timeout", "type"}): [],
    frozenset({"command", "timeoutSec", "type"}): ["copilot"],
}
# shim's command is an interpreter path (PATH_MAX is 4096) and a few arguments,
# quoted. Anything longer is someone else's, and shlex.split is quadratic in a
# long quoted token.
_MAX_COMMAND = 8192


def shim_hook(handler: object) -> tuple[str, str] | None:
    """(interpreter, module) when a hook handler is one shim wrote, else None.

    Every release wrote a `command` handler with exactly the keys above that
    runs `<python> -I -B -m <module>` and the client's argument, and nothing
    else. The interpreter is whichever installation wrote it, so it is not part
    of the identity: matching it made a second installation add a second hook
    set, and revert leave the first one running. Anything that differs in any
    other way, such as a command chained after shim's, is someone's own hook.
    """
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return None
    tail = _LAYOUTS.get(frozenset(handler))
    command = handler.get("command")
    if tail is None or not isinstance(command, str) or len(command) > _MAX_COMMAND:
        return None
    if "args" in handler:
        argv = [command, *handler["args"]] if isinstance(handler["args"], list) else []
    elif HOOK_MODULE in command or LEGACY_HOOK_MODULE in command:
        try:
            argv = shlex.split(command)
        except ValueError:
            return None
        # Only the quoting shlex.join wrote: `/x/$(y)/python` unquoted runs `y`.
        if shlex.join(argv) != command:
            return None
    else:
        return None
    for module in (HOOK_MODULE, LEGACY_HOOK_MODULE):
        if argv[1:] == ["-I", "-B", "-m", module, *tail]:
            return argv[0], module
    return None


def shim_hooks(content: bytes) -> list[tuple[str, str, str]]:
    """(event, interpreter, module) for every handler in the document that runs shim."""
    hooks = _load(content).get("hooks", {})
    assert isinstance(hooks, dict)
    return [
        (event, *found)
        for event, groups in hooks.items()
        for group in groups
        for handler in group["hooks"]
        if (found := shim_hook(handler)) is not None
    ]


def _is_shim(group: dict) -> bool:
    ours = [shim_hook(handler) is not None for handler in group["hooks"]]
    if any(ours) and not all(ours):
        raise ValueError("a hook group runs shim beside another hook")
    return any(ours)


def _same_group(left: object, right: object) -> bool:
    try:
        return json.dumps(
            left, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) == json.dumps(
            right, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except RecursionError as error:
        raise ValueError("invalid hook settings") from error


def _drop_shim_groups(hooks: dict, keep: dict) -> bool:
    """Remove every group that runs shim, except an event's one exact `keep`."""
    changed = False
    for event, groups in list(hooks.items()):
        ours = [group for group in groups if _is_shim(group)]
        exact = len(ours) == 1 and event in keep and _same_group(ours[0], keep[event])
        if not ours or exact:
            continue
        changed = True
        hooks[event] = [group for group in groups if not _is_shim(group)]
        if not hooks[event] and event not in keep:
            del hooks[event]
    return changed


def installed_events(
    content: bytes, registrations: Sequence[Registration]
) -> frozenset:
    hooks = _load(content).get("hooks", {})
    assert isinstance(hooks, dict)
    return frozenset(
        event
        for event, group in registrations
        if any(_same_group(item, group) for item in hooks.get(event, []))
    )


def add_groups(content: bytes | None, registrations: Sequence[Registration]) -> bytes:
    """Leave exactly `registrations` as shim's groups, each appended last if new."""
    if not registrations:
        assert content is not None
        return content
    document = {} if content is None else _load(content)
    hooks = document.setdefault("hooks", {})
    assert isinstance(hooks, dict)
    changed = _drop_shim_groups(hooks, dict(registrations)) or content is None
    for event, group in registrations:
        groups = hooks.setdefault(event, [])
        assert isinstance(groups, list)
        if not any(_same_group(item, group) for item in groups):
            groups.append(group)
            changed = True
    if changed:
        return _dump(document)
    assert content is not None
    return content


def remove_groups(content: bytes) -> bytes:
    """Remove every group that runs shim, whichever installation wrote it."""
    document = _load(content)
    hooks = document.get("hooks")
    if hooks is None:
        return content
    assert isinstance(hooks, dict)
    if not _drop_shim_groups(hooks, {}):
        return content
    if not hooks:
        del document["hooks"]
    return _dump(document)
