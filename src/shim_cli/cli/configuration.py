from __future__ import annotations

import shlex
from pathlib import Path

import typer
from rich import box
from rich.table import Table
from rich.text import Text

from shim_cli.cli.output import (
    FIX_CONFIRMATION,
    Check,
    console,
    emit,
    emit_error,
    emit_json,
    unwritable,
)
from shim_cli.config import (
    MAX_CONFIG_BYTES,
    SettingsRefused,
    config_path,
    describe_settings_error,
    policy_from_state,
    render_settings,
)
from shim_cli.events.diet import DEFAULT_TRANSFORMS
from shim_cli.guard import DEFAULT_ENTITIES, ENTITY_TYPES, normalize_entities
from shim_cli.guard.entities import (
    CUSTOM,
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
    markers: str = "report",
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
            f"Diet: {', '.join(diet) if diet else 'off'}"
            + ("    Markers: note" if markers == "note" else ""),
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
FIX_SETTINGS_INVALID = "Edit the settings file the error names until it parses."
_FIX_CONFLICT = "Run the command with one of them."


def refusal_fix(reason: str, target: Path) -> str:
    """chmod mends a mode; a link, an owner or a shape needs something else."""
    path, folder = shlex.quote(str(target)), shlex.quote(str(target.parent))
    if "symlink" in reason:
        return (
            f"Replace the link {path} with the file it points to, or set "
            "SHIM_CONFIG to that file."
        )
    if "not owned" in reason:
        return (
            f"{path} or a folder above it belongs to another user: have it given "
            f'to you (sudo chown "$(id -un)" {folder} {path}), or set SHIM_CONFIG '
            "to a file you own."
        )
    if "ancestor" in reason:
        return "Set SHIM_CONFIG to a file in a folder only you can write to."
    if "hard-linked" in reason or "regular file" in reason or "limit" in reason:
        return f"Replace {path} with a regular file of its own, 16 KB at most."
    if "changed" in reason:
        return "Run the command again."
    return f"chmod 700 {folder} && chmod 600 {path}"


def settings_check(error: BaseException) -> Check:
    """SETTINGS_REFUSED or SETTINGS_INVALID, as doctor, config and audit say it."""
    detail = describe_settings_error(error)
    if isinstance(error, SettingsRefused):
        return Check(
            "entity_settings",
            "FAIL",
            detail,
            code="SETTINGS_REFUSED",
            fix=refusal_fix(error.reason, config_path()),
        )
    return Check(
        "entity_settings",
        "FAIL",
        detail,
        code="SETTINGS_INVALID",
        fix=FIX_SETTINGS_INVALID,
    )


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
        emit_error(
            "config",
            "SETTINGS_PATH_INVALID",
            "Entity settings path is invalid.",
            "Unset SHIM_CONFIG or set it to an absolute path.",
            as_json=as_json,
        )
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
        emit_error(
            "config",
            "OPTIONS_CONFLICT",
            "--reset cannot be combined with entity options.",
            _FIX_CONFLICT,
            as_json=as_json,
        )
    if only and (enable or disable):
        emit_error(
            "config",
            "OPTIONS_CONFLICT",
            "--only cannot be combined with --enable or --disable.",
            _FIX_CONFLICT,
            as_json=as_json,
        )
    if set(enable).intersection(disable):
        emit_error(
            "config",
            "OPTIONS_CONFLICT",
            "The same entity cannot be enabled and disabled.",
            _FIX_CONFLICT,
            as_json=as_json,
        )

    try:
        if changing:
            ensure_parent(target)
    except (InstallationError, OSError) as error:
        found = unwritable(error, target)
        emit_error(
            "config",
            "SETTINGS_PATH_UNSAFE",
            "Entity settings path is unsafe; nothing was saved.",
            found[1] if found else refusal_fix(str(error), target),
            as_json=as_json,
        )
    state = inspect_file(target, MAX_CONFIG_BYTES)
    # Parse and plan from the same snapshot, before confirmation.
    try:
        policy = policy_from_state(state)
    except ValueError as error:
        policy = None
        if not (reset or only):
            problem = settings_check(error)
            assert problem.code is not None
            emit_error(
                "config", problem.code, problem.detail, problem.fix, as_json=as_json
            )

    try:
        if reset:
            enabled, modes, tool_entities = DEFAULT_ENTITIES, {}, {}
            keep_ledger, keep_diet = False, DEFAULT_TRANSFORMS
            keep_custom: list = []
            keep_reveal: dict = {}
            keep_markers = "report"
        else:
            assert policy is not None or only
            modes = policy.modes if policy else {}
            tool_entities = policy.tool_entities if policy else {}
            keep_ledger = policy.ledger if policy else False
            if ledger is not None:
                keep_ledger = ledger
            keep_diet = policy.diet if policy else DEFAULT_TRANSFORMS
            keep_markers = policy.markers if policy else "report"
            if diet is not None:
                keep_diet = DEFAULT_TRANSFORMS if diet else ()
            try:
                keep_custom = _with_custom(
                    [pattern.entry for pattern in policy.custom] if policy else [],
                    custom,
                    custom_literal,
                    remove_custom,
                )
            except ValueError as error:
                emit_error(
                    "config",
                    "CUSTOM_PATTERN_INVALID",
                    str(error),
                    "Change the pattern, or remove it with "
                    "shim config --remove-custom NAME --yes.",
                    as_json=as_json,
                )
            try:
                keep_reveal = _with_reveal(
                    policy.reveal if policy else {}, reveal, no_reveal
                )
            except ValueError as error:
                emit_error(
                    "config",
                    "REVEAL_INVALID",
                    str(error),
                    "Use --reveal IBAN=N, CREDIT_CARD=N or PHONE=N with N from 1 to 4.",
                    as_json=as_json,
                )
            if only:
                enabled = normalize_entities(set(only))
            else:
                assert policy is not None
                selected = set(policy.entities)
                selected.update(enable)
                if custom or custom_literal:
                    selected.add(CUSTOM)
                selected.difference_update(disable)
                enabled = normalize_entities(selected)
    except ValueError:
        emit_error(
            "config",
            "SETTINGS_INVALID",
            _INVALID,
            FIX_SETTINGS_INVALID,
            as_json=as_json,
        )
    except OSError:
        emit_error(
            "config",
            "SETTINGS_REFUSED",
            _INVALID,
            refusal_fix("", target),
            as_json=as_json,
        )

    if not changing:
        if as_json:
            _emit_settings_json(
                enabled,
                ledger=keep_ledger,
                diet=list(keep_diet),
                custom=keep_custom,
                reveal=keep_reveal,
                markers=keep_markers,
            )
            return
        _show(
            enabled,
            "Current detection",
            keep_ledger,
            keep_diet,
            keep_custom,
            keep_reveal,
            keep_markers,
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
            keep_markers,
        ),
    )
    if as_json and not yes:
        emit_error(
            "config",
            "CONFIRMATION_REQUIRED",
            "--yes is required with --json",
            FIX_CONFIRMATION,
            as_json=True,
        )
    if not as_json:
        _show(
            enabled,
            "New detection",
            keep_ledger,
            keep_diet,
            keep_custom,
            keep_reveal,
            keep_markers,
        )
        emit("WARN", f"File: {target}")
        if not yes and not typer.confirm("Save these settings?", default=False):
            emit("WARN", "Settings unchanged.")
            raise typer.Exit(1)

    try:
        changed = apply(plan)
    except (InstallationError, OSError, ValueError) as error:
        if (found := unwritable(error, target)) is not None:
            reason, fix = found
            emit_error(
                "config",
                "SETTINGS_UNWRITABLE",
                f"Entity settings were not saved: {reason}.",
                fix,
                as_json=as_json,
            )
        emit_error(
            "config",
            "SETTINGS_CHANGED",
            "Entity settings were unsafe or changed; nothing was saved.",
            "Run the command again.",
            as_json=as_json,
        )

    if as_json:
        _emit_settings_json(
            enabled,
            changed=changed,
            ledger=keep_ledger,
            diet=list(keep_diet),
            custom=keep_custom,
            reveal=keep_reveal,
            markers=keep_markers,
        )
    else:
        emit(
            "PASS",
            "Entity settings saved." if changed else "Entity settings already match.",
        )
