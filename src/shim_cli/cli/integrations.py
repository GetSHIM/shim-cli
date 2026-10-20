from __future__ import annotations

import contextlib
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Literal, NoReturn

import typer

from shim_cli.cli.output import (
    FIX_CONFIRMATION,
    emit,
    emit_error,
    emit_json,
    unwritable,
)
from shim_cli.clients.claude import settings as claude_settings
from shim_cli.clients.codex import settings as codex_settings
from shim_cli.clients.copilot import settings as copilot_settings
from shim_cli.clients.hook_settings import (
    HOOK_MODULE,
    LEGACY_HOOK_MODULE,
    interpreter_path,
    remove_groups,
    shim_hooks,
)
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


def hook_argv(client: str) -> list[str]:
    """The command the client runs for shim, read from the fragment install writes.

    Every handler in a fragment runs the same command. Claude Code runs
    `command` with `args`; Codex and Copilot hand `command` to a shell, which
    splits it the way `shlex.join` quoted it.
    """
    hooks = _hook_fragment(client)["hooks"]
    assert isinstance(hooks, dict)
    entry = next(iter(hooks.values()))[0]
    handler = entry["hooks"][0] if "hooks" in entry else entry
    if "args" in handler:
        return [handler["command"], *handler["args"]]
    return shlex.split(handler["command"])


def _refuse_unless_hook_runs(client: str, command: str, as_json: bool) -> None:
    """Run the exact hook command on doctor's fixtures before calling it working.

    The client runs `<python> -I -B -m shim_cli.hook`, and -I ignores the user
    site and PYTHONPATH: a `pip install --user` shim imports in this process
    and not in the hook, which then fails open on every event. Nothing else
    notices, so this is the only guard.
    """
    from shim_cli.cli.diagnostics import _runner_check

    check = _runner_check(client)
    if check.status != "PASS":
        emit_error(
            command,
            "DETECTOR_UNAVAILABLE",
            check.detail,
            check.fix,
            as_json=as_json,
            then=check.fix,
            client=client,
        )


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


def hooks_on_disk(client: str) -> list[tuple[str, str, str]]:
    """(event, interpreter, module) for each shim hook in the client's file.

    Whichever installation wrote them; empty when the file is absent or cannot
    be read, which the plan reports on its own.
    """
    module = {
        "claude": claude_settings,
        "codex": codex_settings,
        "copilot": copilot_settings,
    }[client]
    try:
        state = inspect_file(module.target_path(), module.MAX_CONFIG_BYTES)
        if state.kind is not StateKind.FILE or state.content is None:
            return []
        if client == "copilot":
            return copilot_settings.shim_hooks(state.content) or []
        return shim_hooks(state.content)
    except (OSError, ValueError):
        return []


def other_installations(client: str) -> list[str]:
    """Interpreters of other shim installations whose hooks the client runs."""
    current = str(interpreter_path(sys.executable))
    return sorted(
        {
            interpreter
            for _event, interpreter, hook_module in hooks_on_disk(client)
            if hook_module == HOOK_MODULE and interpreter != current
        }
    )


def existing_hooks(client: str) -> tuple[bool, bool]:
    """(a 0.2.0 fragment is on disk, hooks that are not shim's are on disk).

    `add_hook` already removes the old fragment before appending the new one.
    Only the messages ever said otherwise: an upgrade printed "existing hooks
    will be preserved" over a file whose only hook was the one being replaced.
    """
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
        legacy = any(
            hook_module == LEGACY_HOOK_MODULE
            for _event, _interpreter, hook_module in shim_hooks(state.content)
        )
        stripped = remove_groups(state.content)
    except ValueError:
        return (False, False)
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
        "The file is rewritten with 2-space indentation; no value in it changes."
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


def parse_error(plan: Plan) -> str:
    """Where a client settings file stops parsing, or "" when it parses.

    `json`'s message names the expected token, never the file's content.
    """
    if plan.state.content is None:
        return ""
    try:
        json.loads(plan.state.content.decode("utf-8"))
    except UnicodeDecodeError:
        return f" {plan.target} is not UTF-8."
    except json.JSONDecodeError as error:
        return (
            f" {plan.target} is not valid JSON: {error.msg} at line "
            f"{error.lineno}, column {error.colno}."
        )
    return ""


def unsafe_fix(reason: str, target: Path) -> str:
    """What mends the refusal `reason` names; group-writable is not "not yours"."""
    path, folder = shlex.quote(str(target)), shlex.quote(str(target.parent))
    if "writable by another user" in reason:
        where = (
            f"the folder above {path} that others can write to"
            if "ancestor" in reason
            else folder
            if "parent" in reason
            else path
        )
        return f"Run chmod g-w,o-w {where}"
    if "absolute" in reason:
        return (
            "Point the client's settings folder variable (CLAUDE_CONFIG_DIR, "
            "CODEX_HOME or COPILOT_HOME) at an absolute path without .."
        )
    if "cannot create" in reason:
        return f"Create {folder} yourself, with only you able to write to it"
    return f"Make {path} a regular file owned by you"


def conflict_fix(plan: Plan) -> str:
    target = shlex.quote(str(plan.target))
    if parse_error(plan):
        return f"Fix {target} by hand so it parses"
    return f"Resolve {target} by hand ({plan.message})"


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
    conflict = plan.action is Action.CONFLICT
    syntax = parse_error(plan) if conflict else ""
    text = (
        message + syntax if syntax else f"{message.removesuffix('.')}: {plan.message}."
    )
    fix = (
        f"{conflict_fix(plan) if conflict else unsafe_fix(plan.message, plan.target)}"
        ", then run the command again."
    )
    # The human form shows the fix too: it is what the reader has to do.
    follow = f"{fix} {then}" if then else fix
    if conflict:
        emit_error(
            command,
            "CLIENT_SETTINGS_MALFORMED",
            text,
            fix,
            as_json=as_json,
            then=follow,
            client=client,
            target=target,
            **data,
        )
    emit_error(
        command,
        "CLIENT_SETTINGS_UNSAFE",
        text,
        fix,
        as_json=as_json,
        then=follow,
        client=client,
        target=target,
        **data,
    )


def _not_changed(
    client: str,
    command: str,
    target: Path,
    error: BaseException,
    as_json: bool,
    *,
    permanent: bool = False,
) -> NoReturn:
    """A write refused for good says why; only a race is worth running again.

    `permanent`: the error came from a check of the folders, not from `apply`
    finding the file changed since it was planned.
    """
    name = client_name(client)
    if (found := unwritable(error, target)) is not None:
        reason, fix = found
        emit_error(
            command,
            "CLIENT_SETTINGS_UNWRITABLE",
            f"{name} hook configuration was not changed: {reason}.",
            fix,
            as_json=as_json,
            client=client,
        )
    if permanent and isinstance(error, InstallationError):
        fix = f"{unsafe_fix(str(error), target)}, then run the command again."
        emit_error(
            command,
            "CLIENT_SETTINGS_UNSAFE",
            f"{name} hook configuration was not changed: {error}.",
            fix,
            as_json=as_json,
            then=fix,
            client=client,
            target=str(target),
        )
    emit_error(
        command,
        "CLIENT_SETTINGS_CHANGED",
        f"{name} hook configuration was not changed.",
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


_NEXT_STEP = {
    "claude": "Start a new Claude Code session; /hooks lists shim.",
    "copilot": "Start a new GitHub Copilot CLI session; it has no trust step.",
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
    _refuse_unless_hook_runs(client, "install", as_json)
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
        if foreign_hooks:
            warnings.append(
                f"Existing {name} hooks will be preserved; shim will be appended last."
            )
    if others := other_installations(client):
        warnings.append(
            f"shim's hook from another installation ({', '.join(others)}) will be "
            "replaced by this one."
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
        verb = (
            "create"
            if action is Action.CREATE
            else "append to"
            if foreign_hooks
            else "update"
        )
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
        if foreign_hooks
        else f"Add shim's {name} hook?"
    )
    if not yes and not typer.confirm(prompt, default=False):
        emit("WARN", "Installation cancelled.")
        raise typer.Exit(1)
    if missing_parent:
        try:
            ensure_parent(plan.target)
            plan = client_plan(client, "install")
        except ValueError:
            _plan_error(client, "install", as_json)
        except (InstallationError, OSError) as error:
            _not_changed(client, "install", plan.target, error, as_json, permanent=True)
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
        apply(plan)
    except (InstallationError, OSError) as error:
        _not_changed(client, "install", plan.target, error, as_json)
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
    else:
        print(_NEXT_STEP[client])


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
    if state == "installed":
        _refuse_unless_hook_runs(client, "status", as_json)
    if as_json:
        emit_json("status", "ok", client=client, state=state)
    elif state == "installed":
        emit("PASS", f"{name} hook configuration is installed.")
    elif others := other_installations(client):
        emit(
            "WARN",
            f"{name} runs shim's hook from another installation "
            f"({', '.join(others)}); run shim install {client} to run only this one.",
        )
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
            then=f"Review {name} hooks manually; shim removes only its own hook groups.",
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
            "Only shim's hook groups will be removed; other hooks will be preserved.",
        )
    if not yes and not typer.confirm(f"Remove shim's {name} hook?", default=False):
        emit("WARN", "Revert cancelled.")
        raise typer.Exit(1)
    if plan.action is not Action.NOOP:
        try:
            apply(plan)
        except (InstallationError, OSError) as error:
            _not_changed(client, "revert", plan.target, error, as_json)
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
