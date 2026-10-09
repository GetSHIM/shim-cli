from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any, Literal, NoReturn

import typer

from shim_cli.cli.output import FIX_CONFIRMATION, emit, emit_error, emit_json
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


def _inline_hooks_notice(client: str) -> str | None:
    if client != "codex":
        return None
    try:
        inline_hooks = codex_settings.has_inline_hooks()
    except ValueError:
        return "Codex config.toml could not be inspected and will stay untouched."
    if inline_hooks:
        return "Inline config.toml hooks will stay untouched and may coexist with hooks.json."
    return None


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


def _plan_error(client: str, command: str, as_json: bool) -> NoReturn:
    emit_error(
        command,
        "CLIENT_SETTINGS_UNREADABLE",
        f"Unable to inspect {client_name(client)} hook configuration.",
        f"Run shim doctor {client}.",
        as_json=as_json,
        client=client,
    )


def _plan_refused(
    plan: Plan,
    client: str,
    command: str,
    message: str,
    *,
    as_json: bool,
    then: str | None = None,
    **data: Any,
) -> NoReturn:
    """CONFLICT and REFUSE: shim leaves a file it cannot change safely alone."""
    target = str(plan.target)
    if plan.action is Action.CONFLICT:
        emit_error(
            command,
            "CLIENT_SETTINGS_MALFORMED",
            message,
            f"Fix {target} by hand so it parses, then run the command again.",
            as_json=as_json,
            then=then,
            client=client,
            target=target,
            **data,
        )
    emit_error(
        command,
        "CLIENT_SETTINGS_UNSAFE",
        message,
        f"Make {target} a regular file owned by you, then run the command again.",
        as_json=as_json,
        then=then,
        client=client,
        target=target,
        **data,
    )


def _not_changed(client: str, command: str, as_json: bool) -> NoReturn:
    emit_error(
        command,
        "CLIENT_SETTINGS_CHANGED",
        f"{client_name(client)} hook configuration was not changed.",
        "Run the command again.",
        as_json=as_json,
        client=client,
    )


def _legacy_copilot_file() -> Path | None:
    """The 0.2.0 file when it is ours; a stranger's file of that name is not."""
    legacy = copilot_settings.legacy_target_path()
    state = inspect_file(legacy, copilot_settings.MAX_CONFIG_BYTES)
    if state.kind is not StateKind.FILE or state.content is None:
        return None
    return legacy if copilot_settings.is_ours(state.content) else None


def _remove_legacy_copilot_file(as_json: bool) -> bool:
    legacy = _legacy_copilot_file()
    if legacy is None:
        return False
    try:
        legacy.unlink()
    except OSError:
        # Saying "removed" over a failed unlink is the same defect this branch
        # has been fixing everywhere else: a message that does not match what
        # happened. The file is still there and doctor will keep naming it.
        return False
    if not as_json:
        emit("PASS", f"removed the old hook file at {legacy}")
    return True


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


FIX_REINSTALL = (
    "Reinstall shim: uv tool install --reinstall shim, or pipx reinstall shim."
)
_NEXT_STEP = {
    "claude": "Start a new Claude Code session; /hooks lists shim.",
    "codex": (
        "Codex skips a hook you have not trusted, without warning: open "
        "/hooks in Codex, review the shim entry and enable it."
    ),
}


def install(*, client: str, dry_run: bool, yes: bool, as_json: bool = False) -> None:
    from shim_cli.cli import migration

    if not dry_run:
        migration.announce(
            migration.settings() + migration.ledger_files(), as_json=as_json
        )
    name = client_name(client)
    try:
        plan = client_plan(client, "install")
    except (OSError, ValueError):
        _plan_error(client, "install", as_json)

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
        _plan_refused(
            plan,
            client,
            "install",
            f"{name} hook configuration cannot be changed safely.",
            as_json=as_json,
            then=f"Review {name} hooks manually; shim did not change malformed, "
            "ambiguous, or unsafe settings.",
        )
    fragment = _hook_fragment(client)
    hooks = fragment["hooks"]
    assert isinstance(hooks, dict)
    result: dict = {
        "client": client,
        "target": str(plan.target),
        "action": action.value,
        "events": [] if action is Action.NOOP else sorted(hooks),
    }
    if action is Action.NOOP and not as_json:
        emit("PASS", f"shim is already installed for {name}.")
        return
    # Read before apply() rewrites the file; both messages below describe it.
    legacy_hook, foreign_hooks = existing_hooks(client)
    warnings: list[str] = []
    if action is Action.UPDATE:
        if legacy_hook and not as_json:
            emit("PASS", "Replaced the 0.2.0 hook line with the current one.")
        if foreign_hooks or not legacy_hook:
            warnings.append(
                f"Existing {name} hooks will be preserved; shim will be appended last."
            )
    if action is not Action.NOOP and (notice := _inline_hooks_notice(client)):
        warnings.append(notice)
    if not as_json:
        for warning in warnings:
            emit("WARN", warning)
    if dry_run:
        if as_json:
            emit_json(
                "install",
                "ok",
                **result,
                fragment=fragment,
                summary=_fragment_summary(client),
                dry_run=True,
            )
            return
        verb = "create" if action is Action.CREATE else "append to"
        emit("WARN", f"Would {verb} {name} hooks at {plan.target} with this fragment:")
        emit("WARN", _fragment_summary(client))
        print(json.dumps(fragment, ensure_ascii=False, indent=2))
        return
    result.update(
        replaced_legacy=action is Action.UPDATE and legacy_hook,
        preserved_hooks=foreign_hooks,
        warnings=warnings,
        next_step=None,
    )
    if action is Action.NOOP:
        emit_json("install", "ok", **result)
        return
    if as_json and not yes:
        emit_error(
            "install",
            "CONFIRMATION_REQUIRED",
            "--yes is required with --json",
            FIX_CONFIRMATION,
            as_json=True,
            client=client,
        )
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
            _not_changed(client, "install", as_json)
        if plan.action is Action.NOOP:
            if as_json:
                emit_json("install", "ok", **{**result, "action": "noop", "events": []})
            else:
                emit("PASS", f"shim is already installed for {name}.")
            return
        if plan.action is not Action.CREATE:
            emit_error(
                "install",
                "CLIENT_SETTINGS_CHANGED",
                f"{name} hook configuration changed before install.",
                "Run the command again.",
                as_json=as_json,
                client=client,
            )
    try:
        from shim_cli.guard import evaluate

        evaluate("Synthetic safe prompt")
    except Exception:
        emit_error(
            "install",
            "DETECTOR_UNAVAILABLE",
            "shim detector could not start.",
            FIX_REINSTALL,
            as_json=as_json,
            client=client,
        )
    try:
        apply(plan)
    except (InstallationError, OSError):
        _not_changed(client, "install", as_json)
    if client == "copilot" and _remove_legacy_copilot_file(as_json):
        result["replaced_legacy"] = True
    if as_json:
        emit_json("install", "ok", **{**result, "next_step": _NEXT_STEP.get(client)})
        return
    emit(
        "PASS",
        f"Appended shim after existing {name} hooks."
        if action is Action.UPDATE and foreign_hooks
        else f"Installed shim for {name}.",
    )
    if client == "codex":
        emit("WARN", _NEXT_STEP["codex"])


def status(*, client: str, as_json: bool) -> None:
    name = client_name(client)
    try:
        plan = client_plan(client, "install")
        label, state = plan_status(plan)
    except (OSError, ValueError):
        _plan_error(client, "status", as_json)
    if label == "FAIL":
        _plan_refused(
            plan,
            client,
            "status",
            f"{name} hook configuration is unsafe or differs from shim.",
            as_json=as_json,
            state=state,
        )
    if as_json:
        emit_json("status", "ok", client=client, state=state)
    elif state == "installed":
        emit("PASS", f"{name} hook configuration is installed.")
    else:
        emit("WARN", f"{name} hook configuration is not installed.")
    if label == "WARN":
        raise typer.Exit(1)


def revert(*, client: str, yes: bool, as_json: bool = False) -> None:
    name = client_name(client)
    try:
        plan = client_plan(client, "revert")
    except (OSError, ValueError):
        _plan_error(client, "revert", as_json)
    if plan.action in {Action.CONFLICT, Action.REFUSE}:
        _plan_refused(
            plan,
            client,
            "revert",
            f"{name} hook configuration cannot be removed safely.",
            as_json=as_json,
            then=f"Review {name} hooks manually; shim removes only its exact hook group.",
        )
    legacy = client == "copilot" and _legacy_copilot_file() is not None
    result: dict = {
        "client": client,
        "target": str(plan.target),
        "action": "noop",
        "removed_legacy_file": False,
        "deleted_file": None,
    }
    if plan.action is Action.NOOP and not legacy:
        if as_json:
            emit_json("revert", "ok", **result)
        else:
            emit("PASS", f"shim is not installed for {name}.")
        return
    if as_json and not yes:
        emit_error(
            "revert",
            "CONFIRMATION_REQUIRED",
            "--yes is required with --json",
            FIX_CONFIRMATION,
            as_json=True,
            client=client,
        )
    if not as_json:
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
            _not_changed(client, "revert", as_json)
    result["action"] = "remove"
    if client == "copilot":
        result["removed_legacy_file"] = _remove_legacy_copilot_file(as_json)
        # The file is shim's own. Leaving `{"version": 1, "hooks": {}}` behind
        # is litter, not preservation.
        removed = _remove_empty_copilot_file()
        if removed:
            result["deleted_file"] = str(removed)
    if as_json:
        emit_json("revert", "ok", **result)
    elif result["deleted_file"]:
        emit("PASS", f"Removed shim and deleted {result['deleted_file']}.")
    else:
        emit("PASS", f"Removed shim and preserved the {name} settings file.")
