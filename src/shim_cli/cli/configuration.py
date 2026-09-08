from __future__ import annotations

from typing import NoReturn

import typer
from rich import box
from rich.table import Table
from rich.text import Text

from shim_cli.cli.output import console, emit, emit_json
from shim_cli.config import (
    MAX_CONFIG_BYTES,
    config_path,
    policy_from_state,
    render_settings,
)
from shim_cli.events.diet import DEFAULT_TRANSFORMS
from shim_cli.guard import DEFAULT_ENTITIES, ENTITY_TYPES, normalize_entities
from shim_cli.guard.entities import (
    compile_custom,
    entry_source,
    normalize_reveal,
    unsafe_pattern,
)
from shim_cli.settings_files import (
    InstallationError,
    apply,
    ensure_parent,
    inspect_file,
    plan_change,
)


def _pair(text: str) -> tuple[str, str]:
    name, separator, value = text.partition("=")
    if not separator:
        raise ValueError("a custom pattern needs NAME=VALUE")
    return name.strip(), value


def _with_custom(
    existing: list,
    patterns: tuple[str, ...],
    literals: tuple[str, ...],
    removed: tuple[str, ...],
) -> list:
    entries = [dict(entry) for entry in existing]
    for text, key in [(item, "pattern") for item in patterns] + [
        (item, "literal") for item in literals
    ]:
        name, value = _pair(text)
        entry = {"name": name, key: value}
        compile_custom([entry])
        reason = unsafe_pattern(name, entry_source(entry))
        if reason:
            raise ValueError(reason)
        entries = [item for item in entries if item.get("name") != name] + [entry]
    dropped = {name.strip() for name in removed}
    entries = [item for item in entries if item.get("name") not in dropped]
    compile_custom(entries)
    return entries


def _with_reveal(existing: dict, added: tuple[str, ...], removed: tuple[str, ...]):
    reveal = dict(existing)
    for text in added:
        name, value = _pair(text)
        if not value.isdecimal():
            raise ValueError("a reveal length must be a number")
        reveal[name] = int(value)
    for name in removed:
        reveal.pop(name.strip(), None)
    return normalize_reveal(reveal)


def _show(
    enabled: tuple[str, ...],
    title: str,
    ledger: bool,
    diet: tuple[str, ...],
    custom: list | None = None,
    reveal: dict | None = None,
) -> None:
    selected = set(enabled)
    output = console()
    count = f"{len(enabled)}/{len(ENTITY_TYPES)}"
    heading = (
        f"Entities: {count} on" if output.width < 30 else f"{title}: {count} enabled"
    )
    output.print(Text(heading, style="bold"))
    if output.width < 30:
        for entity in ENTITY_TYPES:
            state = "ON " if entity in selected else "OFF "
            output.print(
                Text(state + entity, style="green" if entity in selected else "dim")
            )
    else:
        table = Table(box=box.SIMPLE, pad_edge=False)
        table.add_column("Entity", overflow="fold")
        table.add_column("Status", no_wrap=True)
        for entity in ENTITY_TYPES:
            status = (
                Text("ON", style="green")
                if entity in selected
                else Text("OFF", style="dim")
            )
            table.add_row(entity, status)
        output.print(table)
    output.print(
        Text(
            f"Ledger: {'on' if ledger else 'off'}    "
            f"Diet: {', '.join(diet) if diet else 'off'}",
            style="dim",
        )
    )
    if custom:
        names = ", ".join(str(entry.get("name")) for entry in custom)
        output.print(Text(f"Custom: {names}", style="dim"))
    if reveal:
        shown = ", ".join(f"{name} {count}" for name, count in reveal.items())
        output.print(Text(f"Reveal: last {shown} digits", style="dim"))
    if not enabled:
        emit("WARN", "All sensitive-data detection is disabled.")


_INVALID = (
    "Entity settings are invalid or unsafe. Reset malformed contents; "
    "review unsafe paths manually."
)


def _fail(
    as_json: bool, message: str = "Unable to update entity settings."
) -> NoReturn:
    if as_json:
        emit_json("config", "error", error="unable to process entity settings")
    else:
        emit("FAIL", message, error=True)
    raise typer.Exit(2)


def _emit_settings_json(enabled: tuple[str, ...], **data: object) -> None:
    selected = set(enabled)
    emit_json(
        "config",
        "ok",
        **data,
        enabled_entities=list(enabled),
        disabled_entities=[entity for entity in ENTITY_TYPES if entity not in selected],
    )


def configure(
    *,
    only: tuple[str, ...],
    enable: tuple[str, ...],
    disable: tuple[str, ...],
    reset: bool,
    ledger: bool | None,
    diet: bool | None,
    custom: tuple[str, ...] = (),
    custom_literal: tuple[str, ...] = (),
    remove_custom: tuple[str, ...] = (),
    reveal: tuple[str, ...] = (),
    no_reveal: tuple[str, ...] = (),
    yes: bool,
    as_json: bool,
) -> None:
    from shim_cli.cli import migration

    migration.announce(migration.settings(), as_json=as_json)
    try:
        target = config_path()
    except ValueError:
        _fail(as_json, "Entity settings path is invalid.")
    changing = bool(
        only
        or enable
        or disable
        or reset
        or ledger is not None
        or diet is not None
        or custom
        or custom_literal
        or remove_custom
        or reveal
        or no_reveal
    )
    if reset and (only or enable or disable):
        _fail(as_json, "--reset cannot be combined with entity options.")
    if only and (enable or disable):
        _fail(as_json, "--only cannot be combined with --enable or --disable.")
    if set(enable).intersection(disable):
        _fail(as_json, "The same entity cannot be enabled and disabled.")

    try:
        if changing:
            ensure_parent(target)
    except (InstallationError, OSError):
        _fail(as_json, "Entity settings path is unsafe; nothing was saved.")
    state = inspect_file(target, MAX_CONFIG_BYTES)
    # Parse and plan from the same snapshot, before confirmation.
    try:
        policy = policy_from_state(state)
    except ValueError:
        policy = None
    if policy is None and not (reset or only):
        _fail(
            as_json,
            "Entity settings are invalid or unsafe. Reset malformed contents; review unsafe paths manually.",
        )

    try:
        if reset:
            enabled, modes, tool_entities = DEFAULT_ENTITIES, {}, {}
            keep_ledger, keep_diet = False, DEFAULT_TRANSFORMS
            keep_custom: list = []
            keep_reveal: dict = {}
        else:
            assert policy is not None or only
            modes = policy.modes if policy else {}
            tool_entities = policy.tool_entities if policy else {}
            keep_ledger = policy.ledger if policy else False
            if ledger is not None:
                keep_ledger = ledger
            keep_diet = policy.diet if policy else DEFAULT_TRANSFORMS
            if diet is not None:
                keep_diet = DEFAULT_TRANSFORMS if diet else ()
            keep_custom = _with_custom(
                [pattern.entry for pattern in policy.custom] if policy else [],
                custom,
                custom_literal,
                remove_custom,
            )
            keep_reveal = _with_reveal(
                policy.reveal if policy else {}, reveal, no_reveal
            )
            if only:
                enabled = normalize_entities(set(only))
            else:
                assert policy is not None
                selected = set(policy.entities)
                selected.update(enable)
                selected.difference_update(disable)
                enabled = normalize_entities(selected)
    except ValueError as error:
        specific = bool(custom or custom_literal or reveal)
        _fail(as_json, str(error) if specific else _INVALID)
    except OSError:
        _fail(as_json, _INVALID)

    if not changing:
        if as_json:
            _emit_settings_json(
                enabled,
                ledger=keep_ledger,
                diet=list(keep_diet),
                custom=keep_custom,
                reveal=keep_reveal,
            )
            return
        _show(
            enabled,
            "Current detection",
            keep_ledger,
            keep_diet,
            keep_custom,
            keep_reveal,
        )
        emit("PASS", f"File: {target}")
        return

    plan = plan_change(
        target,
        state,
        render_settings(
            enabled,
            modes,
            tool_entities,
            keep_ledger,
            keep_diet,
            keep_custom,
            keep_reveal,
        ),
    )
    if as_json and not yes:
        _fail(True)
    if not as_json:
        _show(
            enabled,
            "New detection",
            keep_ledger,
            keep_diet,
            keep_custom,
            keep_reveal,
        )
        emit("WARN", f"File: {target}")
        if not yes and not typer.confirm("Save these settings?", default=False):
            emit("WARN", "Settings unchanged.")
            raise typer.Exit(1)

    try:
        changed = apply(plan)
    except (InstallationError, OSError, ValueError):
        _fail(as_json, "Entity settings were unsafe or changed; nothing was saved.")

    if as_json:
        _emit_settings_json(
            enabled,
            changed=changed,
            ledger=keep_ledger,
            diet=list(keep_diet),
            custom=keep_custom,
            reveal=keep_reveal,
        )
    else:
        emit(
            "PASS",
            "Entity settings saved." if changed else "Entity settings already match.",
        )
