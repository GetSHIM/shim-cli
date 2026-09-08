from __future__ import annotations

import json
import math
from collections.abc import Sequence

MAX_SETTINGS_BYTES = 1_000_000

Registration = tuple


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


def _same_group(left: object, right: object) -> bool:
    try:
        return json.dumps(
            left, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) == json.dumps(
            right, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except RecursionError as error:
        raise ValueError("invalid hook settings") from error


def _match(groups: list, group: dict[str, object]) -> int | None:
    matches = [index for index, item in enumerate(groups) if _same_group(item, group)]
    if len(matches) > 1:
        raise ValueError("duplicate SHIM hook groups are ambiguous")
    return matches[0] if matches else None


def add_groups(content: bytes | None, registrations: Sequence[Registration]) -> bytes:
    if not registrations:
        assert content is not None
        return content
    document = {} if content is None else _load(content)
    hooks = document.setdefault("hooks", {})
    assert isinstance(hooks, dict)
    changed = content is None
    for event, group in registrations:
        groups = hooks.setdefault(event, [])
        assert isinstance(groups, list)
        if _match(groups, group) is None:
            groups.append(group)
            changed = True
    if changed:
        return _dump(document)
    assert content is not None
    return content


def remove_groups(content: bytes, registrations: Sequence[Registration]) -> bytes:
    if not registrations:
        return content
    document = _load(content)
    hooks = document.get("hooks")
    if hooks is None:
        return content
    assert isinstance(hooks, dict)
    changed = False
    for event, group in registrations:
        groups = hooks.get(event, [])
        assert isinstance(groups, list)
        match = _match(groups, group)
        if match is None:
            continue
        del groups[match]
        changed = True
        if not groups:
            del hooks[event]
    if not changed:
        return content
    if not hooks:
        del document["hooks"]
    return _dump(document)
