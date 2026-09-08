from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shim_cli.settings_files import FileState

from shim_cli import policy
from shim_cli.guard import entities as entity_catalog

try:  # pragma: no cover
    import tomllib  # ty: ignore[unresolved-import]
except ModuleNotFoundError:
    import tomli as tomllib

MAX_CONFIG_BYTES = 16_384


def _default_path(directory: str, home: Path | None) -> Path:
    if home is not None:
        return Path(home) / ".config" / directory / "config.toml"
    if configured := os.environ.get("XDG_CONFIG_HOME"):
        return Path(configured).expanduser() / directory / "config.toml"
    return Path.home() / ".config" / directory / "config.toml"


def config_path(home: Path | None = None) -> Path:
    try:
        if home is None and (
            configured := os.environ.get("SHIM_CONFIG")
            or os.environ.get("SHIM_GUARD_CONFIG")
        ):
            target = Path(configured).expanduser()
        else:
            target = _default_path("shim", home)
    except RuntimeError as error:
        raise ValueError("shim settings path is invalid") from error
    return _validated_path(target)


def legacy_config_path(home: Path | None = None) -> Path | None:
    """The 0.2.0 location, or None when a variable pins the path."""
    if home is None and (
        os.environ.get("SHIM_CONFIG") or os.environ.get("SHIM_GUARD_CONFIG")
    ):
        return None
    try:
        return _validated_path(_default_path("shim-guard", home))
    except (RuntimeError, ValueError):
        return None


def _validated_path(path: Path) -> Path:
    target = Path(path)
    if (
        not target.is_absolute()
        or ".." in target.parts
        or not str(target).isprintable()
    ):
        raise ValueError("shim settings path is invalid")
    return target


def render_settings(
    entities: Iterable[str],
    modes: dict | None = None,
    tool_entities: dict | None = None,
    ledger: bool = False,
    diet: tuple | None = None,
    custom: list | None = None,
    reveal: dict | None = None,
) -> bytes:
    import tomli_w

    document: dict = {
        "enabled_entities": list(entity_catalog.normalize_entities(entities))
    }
    if ledger:
        document["ledger"] = True
    if diet is not None:
        from shim_cli.events.diet import DEFAULT_TRANSFORMS

        if not diet:
            document["diet"] = False
        elif tuple(diet) != tuple(DEFAULT_TRANSFORMS):
            document["diet"] = list(diet)
    if tool_entities:
        document["entities"] = {
            key: list(value) for key, value in sorted(tool_entities.items())
        }
    if modes:
        document["mode"] = dict(sorted(modes.items()))
    if custom:
        entity_catalog.compile_custom(custom)
        document["custom"] = [dict(entry) for entry in custom]
    if reveal:
        document["reveal"] = entity_catalog.normalize_reveal(reveal)
    return tomli_w.dumps(document).encode()


def render_entities(entities: Iterable[str]) -> bytes:
    return render_settings(entities)


_TOP_LEVEL = {
    "enabled_entities",
    "mode",
    "entities",
    "ledger",
    "diet",
    "custom",
    "reveal",
}


def _modes(document: dict) -> dict:
    section = document.get("mode", {})
    if not isinstance(section, dict):
        raise ValueError("shim settings are invalid")
    modes = {}
    for key, value in section.items():
        if not isinstance(value, str) or value not in policy.MODES:
            raise ValueError("shim settings are invalid")
        if key in policy.OBSERVE_ONLY and value != policy.OBSERVE:
            raise ValueError("shim settings are invalid")
        modes[key] = value
    return modes


def _custom(document: dict) -> list:
    section = document.get("custom", [])
    if not isinstance(section, list):
        raise ValueError("shim settings are invalid")
    try:
        entity_catalog.compile_custom(section)
    except ValueError as error:
        # `shim config` reports the specific reason; the file itself fails closed.
        raise ValueError("shim settings are invalid") from error
    return [dict(entry) for entry in section]


def _reveal(document: dict) -> dict:
    try:
        return entity_catalog.normalize_reveal(document.get("reveal", {}))
    except ValueError as error:
        # `shim config` reports the specific reason; the file itself fails closed.
        raise ValueError("shim settings are invalid") from error


def _tool_entities(document: dict) -> dict:
    section = document.get("entities", {})
    if not isinstance(section, dict):
        raise ValueError("shim settings are invalid")
    scoped = {}
    for key, value in section.items():
        if not isinstance(value, list):
            raise ValueError("shim settings are invalid")
        scoped[key] = entity_catalog.normalize_entities(value)
    return scoped


def _diet(document: dict) -> tuple:
    from shim_cli.events.diet import DEFAULT_TRANSFORMS, TRANSFORMS

    value = document.get("diet", True)
    if value is True:
        return DEFAULT_TRANSFORMS
    if value is False:
        return ()
    if not isinstance(value, list) or any(name not in TRANSFORMS for name in value):
        raise ValueError("shim settings are invalid")
    return tuple(name for name in TRANSFORMS if name in value)


def parse_settings(text: str) -> dict:
    try:
        document = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError) as error:
        raise ValueError("shim settings are invalid") from error
    if not set(document) <= _TOP_LEVEL:
        raise ValueError("shim settings are invalid")
    enabled = document.get("enabled_entities", list(entity_catalog.DEFAULT_ENTITIES))
    if not isinstance(enabled, list):
        raise ValueError("shim settings are invalid")
    ledger = document.get("ledger", False)
    if not isinstance(ledger, bool):
        raise ValueError("shim settings are invalid")
    return {
        "enabled_entities": list(enabled),
        "mode": _modes(document),
        "entities": _tool_entities(document),
        "ledger": ledger,
        "diet": _diet(document),
        "custom": _custom(document),
        "reveal": _reveal(document),
    }


def load_policy(path: Path | None = None) -> policy.Policy:
    from shim_cli.settings_files import StateKind, inspect_file

    if path is not None:
        return policy_from_state(inspect_file(_validated_path(path), MAX_CONFIG_BYTES))
    state = inspect_file(config_path(), MAX_CONFIG_BYTES)
    # The hook reads the 0.2.0 file until a CLI command moves it. It never writes.
    if state.kind is StateKind.ABSENT and (legacy := legacy_config_path()) is not None:
        state = inspect_file(legacy, MAX_CONFIG_BYTES)
    return policy_from_state(state)


def policy_from_state(state: FileState) -> policy.Policy:
    from shim_cli.settings_files import StateKind

    if state.kind is StateKind.ABSENT:
        from shim_cli.events.diet import DEFAULT_TRANSFORMS

        return policy.Policy(
            entity_catalog.DEFAULT_ENTITIES,
            {},
            {},
            False,
            DEFAULT_TRANSFORMS,
            (),
            {},
        )
    if state.kind is not StateKind.FILE or state.content is None:
        raise ValueError("shim settings cannot be read safely")
    try:
        document = parse_settings(state.content.decode("utf-8"))
    except (UnicodeDecodeError, RecursionError) as error:
        raise ValueError("shim settings are invalid") from error
    try:
        return policy.Policy(
            entity_catalog.normalize_entities(document["enabled_entities"]),
            document["mode"],
            document["entities"],
            document["ledger"],
            document["diet"],
            entity_catalog.compile_custom(document["custom"]),
            document["reveal"],
        )
    except ValueError as error:
        raise ValueError("shim settings are invalid") from error


def load_entities(path: Path | None = None) -> tuple[str, ...]:
    return load_policy(path).entities
