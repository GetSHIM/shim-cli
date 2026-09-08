from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Literal, NoReturn

import typer

from shim_cli.cli.output import emit, emit_json
from shim_cli.clients.claude import settings as claude_settings
from shim_cli.clients.codex import settings as codex_settings
from shim_cli.clients.copilot import settings as copilot_settings
from shim_cli.clients.hook_settings import interpreter_path
from shim_cli.settings_files import (
    Action,
    InstallationError,
    Plan,
    StateKind,
    apply,
    ensure_parent,
    inspect_file,
    plan_change,
)


def client_name(client: str) -> str:
    if client == "claude":
        return "Claude Code"
    if client == "codex":
        return "Codex"
    if client == "copilot":
        return "GitHub Copilot CLI"
    raise ValueError("unsupported client")


def client_plan(client: str, operation: Literal["install", "revert"]) -> Plan:
    if client == "claude":
        target = claude_settings.target_path()
        limit = claude_settings.MAX_CONFIG_BYTES
        add_hook = claude_settings.add_hook
        remove_hook = claude_settings.remove_hook
    elif client == "codex":
        target = codex_settings.target_path()
        limit = codex_settings.MAX_CONFIG_BYTES
        add_hook = codex_settings.add_hook
        remove_hook = codex_settings.remove_hook
    elif client == "copilot":
        target = copilot_settings.target_path()
        limit = copilot_settings.MAX_CONFIG_BYTES
        add_hook = copilot_settings.add_hook
        remove_hook = copilot_settings.remove_hook
    else:
        raise ValueError("unsupported client")
    state = inspect_file(target, limit)
    if state.kind is StateKind.UNSAFE:
        return plan_change(target, state, None)
    if state.kind is StateKind.ABSENT:
        expected = add_hook(None) if operation == "install" else None
        return plan_change(target, state, expected)
    assert state.content is not None
    try:
        expected = (
            add_hook(state.content)
            if operation == "install"
            else remove_hook(state.content)
        )
    except ValueError as error:
        return plan_change(target, state, state.content, conflict=str(error))
    return plan_change(target, state, expected)


def _hook_fragment(client: str) -> dict[str, object]:
    if client == "copilot":
        return copilot_settings.hook_document()
    if client == "claude":
        registrations = claude_settings.hook_groups()
    elif client == "codex":
        registrations = codex_settings.hook_groups()
    else:
        raise ValueError("unsupported client")
    hooks: dict[str, list] = {}
    for event, group in registrations:
        hooks.setdefault(event, []).append(group)
    return {"hooks": hooks}


def _inline_hooks_notice(client: str) -> None:
    if client != "codex":
        return
    try:
        inline_hooks = codex_settings.has_inline_hooks()
    except ValueError:
        emit(
            "WARN", "Codex config.toml could not be inspected and will stay untouched."
        )
        return
    if inline_hooks:
        emit(
            "WARN",
            "Inline config.toml hooks will stay untouched and may coexist with hooks.json.",
        )


def plan_status(plan: Plan) -> tuple[str, str]:
    if plan.action is Action.NOOP:
        return "PASS", "installed"
    if plan.state.kind is StateKind.ABSENT or plan.action is Action.UPDATE:
        return "WARN", "not_installed"
    return "FAIL", "unsafe" if plan.action is Action.REFUSE else "conflict"


def existing_hooks(client: str) -> tuple[bool, bool]:
    """(a 0.2.0 fragment is on disk, hooks that are not shim's are on disk).

    `add_hook` already removes the old fragment before appending the new one.
    Only the messages ever said otherwise: an upgrade printed "existing hooks
    will be preserved" over a file whose only hook was the one being replaced.
    """
    from shim_cli.clients.hook_settings import remove_groups

    module = {"claude": claude_settings, "codex": codex_settings}.get(client)
    if module is None:
        return (False, False)
    try:
        state = inspect_file(module.target_path(), module.MAX_CONFIG_BYTES)
    except (OSError, ValueError):
        return (False, False)
    if state.kind is not StateKind.FILE or state.content is None:
        return (False, False)
    try:
        without_legacy = remove_groups(state.content, module.legacy_hook_groups())
        stripped = remove_groups(without_legacy, module.hook_groups())
    except ValueError:
        return (False, False)
    legacy = without_legacy != state.content
    try:
        document = json.loads(stripped.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return (legacy, False)
    hooks = document.get("hooks") if isinstance(document, dict) else None
    return (legacy, bool(hooks))


def _fragment_summary(client: str) -> str:
    """What the JSON below it means, for someone who will not read the JSON.

    The preview is the last thing a person sees before shim edits a settings
    file they own, and it was 99 lines of raw JSON with no sentence over it.
    """
    fragment = _hook_fragment(client)
    hooks = fragment.get("hooks")
    events = list(hooks) if isinstance(hooks, dict) else []
    count = len(events)
    entry, each = ("entry", "") if count == 1 else ("entries", "each ")
    named = ", ".join(events)
    interpreter = interpreter_path(sys.executable)
    return (
        f"Would add {count} hook {entry} ({named}), "
        f"{each}running {interpreter} -m shim_cli.hook {client}. "
        "Nothing else in the file changes."
    )


def _plan_error(client: str, command: str, as_json: bool = False) -> NoReturn:
    name = client_name(client)
    if as_json:
        emit_json(
            command,
            "error",
            client=client,
            error=f"unable to inspect {name} hook configuration",
        )
    else:
        emit("FAIL", f"Unable to inspect {name} hook configuration.", error=True)
    raise typer.Exit(2)


def _legacy_copilot_file() -> Path | None:
    """The 0.2.0 file when it is ours; a stranger's file of that name is not."""
    legacy = copilot_settings.legacy_target_path()
    state = inspect_file(legacy, copilot_settings.MAX_CONFIG_BYTES)
    if state.kind is not StateKind.FILE or state.content is None:
        return None
    return legacy if copilot_settings.is_ours(state.content) else None


def _remove_legacy_copilot_file() -> None:
    legacy = _legacy_copilot_file()
    if legacy is None:
        return
    with contextlib.suppress(OSError):
        legacy.unlink()
    emit("PASS", f"removed the old hook file at {legacy}")


def _remove_empty_copilot_file() -> Path | None:
    """Copilot's hook file belongs to shim alone; an empty one is litter."""
    target = copilot_settings.target_path()
    try:
        document = json.loads(target.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("hooks"):
        return None
    if set(document) - {"version", "hooks"}:
        return None
    with contextlib.suppress(OSError):
        target.unlink()
        return target
    return None


def install(*, client: str, dry_run: bool, yes: bool) -> None:
    from shim_cli.cli import migration

    if not dry_run:
        migration.announce(
            migration.settings() + migration.ledger_files(), as_json=False
        )
    name = client_name(client)
    try:
        plan = client_plan(client, "install")
    except (OSError, ValueError):
        _plan_error(client, "install")

    missing_parent = (
        plan.action is Action.REFUSE
        and plan.state.kind is StateKind.ABSENT
        and not plan.target.parent.exists()
    )
    action = (
        Action.CREATE
        if missing_parent or (client == "copilot" and plan.action is Action.UPDATE)
        else plan.action
    )
    if action in {Action.CONFLICT, Action.REFUSE}:
        emit("FAIL", f"{name} hook configuration cannot be changed safely.", error=True)
        emit(
            "WARN",
            f"Review {name} hooks manually; shim did not change malformed, ambiguous, or unsafe settings.",
            error=True,
        )
        raise typer.Exit(2)
    if action is Action.NOOP:
        emit("PASS", f"shim is already installed for {name}.")
        return
    # Read before apply() rewrites the file; both messages below describe it.
    legacy_hook, foreign_hooks = existing_hooks(client)
    if action is Action.UPDATE:
        legacy, foreign = legacy_hook, foreign_hooks
        if legacy:
            emit("PASS", "Replaced the 0.2.0 hook line with the current one.")
        if foreign or not legacy:
            emit(
                "WARN",
                f"Existing {name} hooks will be preserved; shim will be appended last.",
            )
    _inline_hooks_notice(client)
    if dry_run:
        verb = "create" if action is Action.CREATE else "append to"
        emit("WARN", f"Would {verb} {name} hooks at {plan.target} with this fragment:")
        emit("WARN", _fragment_summary(client))
        print(json.dumps(_hook_fragment(client), ensure_ascii=False, indent=2))
        return
    prompt = (
        f"Create shim's {name} hook?"
        if action is Action.CREATE
        else f"Append shim after existing {name} hooks?"
    )
    if not yes and not typer.confirm(prompt, default=False):
        emit("WARN", "Installation cancelled.")
        raise typer.Exit(1)
    if missing_parent:
        try:
            ensure_parent(plan.target)
            plan = client_plan(client, "install")
        except (InstallationError, OSError, ValueError):
            emit("FAIL", f"{name} hook configuration was not changed.", error=True)
            raise typer.Exit(2) from None
        if plan.action is Action.NOOP:
            emit("PASS", f"shim is already installed for {name}.")
            return
        if plan.action is not Action.CREATE:
            emit(
                "FAIL", f"{name} hook configuration changed before install.", error=True
            )
            raise typer.Exit(2)
    try:
        from shim_cli.guard import evaluate

        evaluate("Synthetic safe prompt")
    except Exception:
        emit("FAIL", "shim detector could not start.", error=True)
        raise typer.Exit(2) from None
    try:
        apply(plan)
    except (InstallationError, OSError):
        emit("FAIL", f"{name} hook configuration was not changed.", error=True)
        raise typer.Exit(2) from None
    if client == "copilot":
        _remove_legacy_copilot_file()
    emit(
        "PASS",
        f"Appended shim after existing {name} hooks."
        if action is Action.UPDATE and foreign_hooks
        else f"Installed shim for {name}.",
    )
    if client == "codex":
        emit(
            "WARN",
            "Codex runs a hook only after you trust it: open Codex and accept "
            "the shim hook when asked.",
        )


def status(*, client: str, as_json: bool) -> None:
    name = client_name(client)
    try:
        plan = client_plan(client, "install")
        label, state = plan_status(plan)
    except (OSError, ValueError):
        _plan_error(client, "status", as_json)
    if as_json:
        emit_json(
            "status", "ok" if label != "FAIL" else "error", client=client, state=state
        )
    elif state == "installed":
        emit("PASS", f"{name} hook configuration is installed.")
    elif state == "not_installed":
        emit("WARN", f"{name} hook configuration is not installed.")
    else:
        emit(
            "FAIL",
            f"{name} hook configuration is unsafe or differs from shim.",
            error=True,
        )
    if label == "WARN":
        raise typer.Exit(1)
    if label == "FAIL":
        raise typer.Exit(2)


def revert(*, client: str, yes: bool) -> None:
    name = client_name(client)
    try:
        plan = client_plan(client, "revert")
    except (OSError, ValueError):
        _plan_error(client, "revert")
    if plan.action in {Action.CONFLICT, Action.REFUSE}:
        emit("FAIL", f"{name} hook configuration cannot be removed safely.", error=True)
        emit(
            "WARN",
            f"Review {name} hooks manually; shim removes only its exact hook group.",
            error=True,
        )
        raise typer.Exit(2)
    legacy = client == "copilot" and _legacy_copilot_file() is not None
    if plan.action is Action.NOOP and not legacy:
        emit("PASS", f"shim is not installed for {name}.")
        return
    emit(
        "WARN",
        "Only shim's exact hook group will be removed; other hooks will be preserved.",
    )
    if not yes and not typer.confirm(f"Remove shim's {name} hook?", default=False):
        emit("WARN", "Revert cancelled.")
        raise typer.Exit(1)
    if plan.action is not Action.NOOP:
        try:
            apply(plan)
        except (InstallationError, OSError):
            emit("FAIL", f"{name} hook configuration was not changed.", error=True)
            raise typer.Exit(2) from None
    if client == "copilot":
        _remove_legacy_copilot_file()
        # The file is shim's own. Leaving `{"version": 1, "hooks": {}}` behind
        # is litter, not preservation.
        removed = _remove_empty_copilot_file()
        if removed:
            emit("PASS", f"Removed shim and deleted {removed}.")
            return
    emit("PASS", f"Removed shim and preserved the {name} settings file.")
