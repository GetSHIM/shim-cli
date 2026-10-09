from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import typer
from rich import box
from rich.table import Table

from shim_cli.cli.integrations import (
    FIX_REINSTALL,
    client_name,
    client_plan,
    plan_status,
)
from shim_cli.cli.output import console, emit, emit_json
from shim_cli.cli.resolution import installed_plugins, resolve
from shim_cli.clients.claude import settings as claude_settings
from shim_cli.clients.claude.tool_events import coverage as claude_coverage
from shim_cli.clients.codex import settings as codex_settings
from shim_cli.clients.copilot import settings as copilot_settings
from shim_cli.clients.hook_settings import installed_events, interpreter_path
from shim_cli.settings_files import StateKind, inspect_file


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    detail: str
    code: str | None = None
    fix: str | None = None


def _client_version(
    executable: str, name: str, minimum_text: str, tested_text: str
) -> Check:
    path = shutil.which(executable)
    if path is None:
        return Check(
            executable,
            "FAIL",
            f"{name} executable was not found on PATH.",
            code="CLIENT_NOT_FOUND",
            fix=f"Install {name}, or add the folder holding {executable} to PATH.",
        )
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return Check(
            executable,
            "FAIL",
            f"{name} at {path} could not report its version.",
            code="CLIENT_VERSION_UNKNOWN",
            fix=f"Run {path} --version; reinstall {name} if it fails.",
        )
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout + result.stderr)
    if result.returncode or match is None:
        return Check(
            executable,
            "FAIL",
            f"{name} at {path} has an unrecognized version.",
            code="CLIENT_VERSION_UNKNOWN",
            fix=f"Run {path} --version; reinstall {name} if it fails.",
        )
    version_text = match.group(0)
    version = tuple(int(part) for part in match.groups())
    minimum = tuple(int(part) for part in minimum_text.split("."))
    tested = tuple(int(part) for part in tested_text.split("."))
    if version < minimum:
        return Check(
            executable,
            "FAIL",
            f"{name} {version_text} is older than {minimum_text}.",
            code="CLIENT_TOO_OLD",
            fix=f"Update {name} to {minimum_text} or newer.",
        )
    if version > tested:
        return Check(
            executable,
            "WARN",
            f"{name} {version_text} is newer than tested {tested_text}.",
            code="CLIENT_NEWER_THAN_TESTED",
            fix="Check shim once by hand: a prompt holding ops@example.com "
            "must be reported.",
        )
    return Check(executable, "PASS", f"{name} {version_text} at {path} is tested.")


def _version_check(client: str) -> Check:
    if client == "claude":
        return _client_version(
            "claude",
            "Claude Code",
            claude_settings.MINIMUM_CLAUDE_VERSION,
            claude_settings.TESTED_CLAUDE_VERSION,
        )
    if client == "codex":
        return _client_version(
            "codex",
            "Codex",
            codex_settings.MINIMUM_CODEX_VERSION,
            codex_settings.TESTED_CODEX_VERSION,
        )
    if client == "copilot":
        return _client_version(
            "copilot",
            "GitHub Copilot CLI",
            copilot_settings.MINIMUM_COPILOT_VERSION,
            copilot_settings.TESTED_COPILOT_VERSION,
        )
    raise ValueError("unsupported client")


_FIX_CODEX_HOOKS_UNCHECKED = "Run codex features list; it must show hooks as true."


def _codex_hooks_feature() -> Check:
    path = shutil.which("codex")
    if path is None:
        return Check(
            "hooks_feature",
            "FAIL",
            "Codex hook support was not checked: the executable was not found.",
            code="CODEX_HOOKS_UNCHECKED",
            fix=_FIX_CODEX_HOOKS_UNCHECKED,
        )
    try:
        result = subprocess.run(
            [path, "features", "list"],
            capture_output=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return Check(
            "hooks_feature",
            "FAIL",
            "Codex hook support could not be checked.",
            code="CODEX_HOOKS_UNCHECKED",
            fix=_FIX_CODEX_HOOKS_UNCHECKED,
        )
    enabled = any(
        fields and fields[0] == "hooks" and fields[-1] == "true"
        for fields in map(str.split, result.stdout.splitlines())
    )
    if result.returncode or not enabled:
        return Check(
            "hooks_feature",
            "FAIL",
            "Codex hook support is not enabled.",
            code="CODEX_HOOKS_DISABLED",
            fix="Set hooks = true under [features] in Codex's config.toml "
            "(~/.codex or $CODEX_HOME), or delete that line.",
        )
    return Check("hooks_feature", "PASS", "Codex hook support is enabled.")


def _legacy_state(client: str, fragment: bool) -> Check:
    """R7: name every 0.2.0 shape that is still on disk. Changes nothing."""
    from shim_cli.clients.copilot import settings as copilot_settings
    from shim_cli.config import legacy_config_path
    from shim_cli.session import ledger
    from shim_cli.settings_files import StateKind, inspect_file

    found: list[str] = []
    commands: list[str] = []
    if client == "copilot" and not fragment:
        legacy = copilot_settings.legacy_target_path()
        state = inspect_file(legacy, copilot_settings.MAX_CONFIG_BYTES)
        if state.kind is StateKind.FILE and state.content is not None:
            if copilot_settings.is_ours(state.content):
                found.append(
                    f"Hook file uses the old name at {legacy}; "
                    "run shim install copilot to rename it"
                )
                commands.append("shim install copilot")
    if fragment:
        found.append(
            f"Hook installed in the 0.2.0 shape, which 1.0 does not run; "
            f"run shim install {client}"
        )
        commands.append(f"shim install {client}")
    settings_file = legacy_config_path()
    if settings_file is not None and settings_file.is_file():
        found.append(
            f"Settings are still at {settings_file} and move on the next shim config"
        )
        commands.append("shim config")
    try:
        directory = ledger.legacy_root_path()
    except ledger.LedgerError:
        directory = None
    if directory is not None and any(
        directory.glob(f"{ledger.FILE_PREFIX}*{ledger.FILE_SUFFIX}")
    ):
        found.append(
            f"Ledger files are still in {directory} and move on the next shim report"
        )
        commands.append("shim report")
    if not found:
        return Check("legacy_names", "PASS", "No 0.2.0 names are left on disk.")
    detail = ". ".join(found)
    return Check(
        "legacy_names",
        "FAIL" if fragment else "WARN",
        f"{detail[0].lower()}{detail[1:]}.",
        code="LEGACY_NAMES_PRESENT",
        fix=f"Run {', then '.join(commands)}.",
    )


def _has_legacy_fragment(client: str) -> bool:
    """Claude and Codex carry the fragment in their own settings document;
    Copilot carries it as a whole file under the old name."""
    from shim_cli.clients.claude import settings as claude_settings
    from shim_cli.clients.codex import settings as codex_settings
    from shim_cli.clients.copilot import settings as copilot_settings
    from shim_cli.clients.hook_settings import remove_groups
    from shim_cli.settings_files import StateKind, inspect_file

    if client == "copilot":
        state = inspect_file(
            copilot_settings.legacy_target_path(), copilot_settings.MAX_CONFIG_BYTES
        )
        return (
            state.kind is StateKind.FILE
            and state.content is not None
            and copilot_settings.is_ours(state.content)
            and bool(json.loads(state.content)["hooks"])
        )
    if client == "claude":
        module = claude_settings
    else:
        module = codex_settings
    state = inspect_file(module.target_path(), module.MAX_CONFIG_BYTES)
    if state.kind is not StateKind.FILE or state.content is None:
        return False
    try:
        return (
            remove_groups(state.content, module.legacy_hook_groups()) != state.content
        )
    except ValueError:
        return False


def _hook_state(client: str, on_disk: bool = False) -> Check | None:
    name = client_name(client)
    try:
        plan = client_plan(client, "install")
        label, state = plan_status(plan)
    except (OSError, ValueError):
        return Check(
            "hook_configuration",
            "FAIL",
            f"Could not inspect {name} hook configuration.",
            code="CLIENT_SETTINGS_UNREADABLE",
            fix=f"Make the {name} settings file and its folder readable by you.",
        )
    if state == "installed":
        return Check(
            "hook_configuration", label, f"shim's exact {name} hook group is present."
        )
    if state == "not_installed":
        if on_disk:
            return None
        return Check(
            "hook_configuration",
            label,
            f"shim's {name} hook group is not installed; run shim install {client}.",
            code="HOOK_NOT_INSTALLED",
            fix=f"Run shim install {client}.",
        )
    if state == "conflict":
        return Check(
            "hook_configuration",
            label,
            f"{name} hook configuration needs manual review.",
            code="CLIENT_SETTINGS_MALFORMED",
            fix=f"Fix {plan.target} by hand so it parses; shim install will not "
            "change it until then.",
        )
    return Check(
        "hook_configuration",
        label,
        f"{name} hook configuration cannot be trusted safely.",
        code="CLIENT_SETTINGS_UNSAFE",
        fix=f"Make {plan.target} a regular file owned by you, then run shim "
        f"install {client}.",
    )


def _entity_settings() -> Check:
    from shim_cli.cli.configuration import (
        FIX_SETTINGS_INVALID,
        FIX_SETTINGS_UNSAFE,
        settings_refused,
    )
    from shim_cli.config import describe_settings_error, load_entities
    from shim_cli.guard import ENTITY_TYPES

    try:
        enabled = load_entities()
    except (OSError, ValueError) as error:
        if settings_refused(error):
            return Check(
                "entity_settings",
                "FAIL",
                describe_settings_error(error),
                code="SETTINGS_REFUSED",
                fix=FIX_SETTINGS_UNSAFE,
            )
        return Check(
            "entity_settings",
            "FAIL",
            describe_settings_error(error),
            code="SETTINGS_INVALID",
            fix=FIX_SETTINGS_INVALID,
        )
    if not enabled:
        return Check(
            "entity_settings",
            "WARN",
            "All sensitive-data detection is disabled; review with `shim config`.",
            code="DETECTION_DISABLED",
            fix="Turn types back on with shim config --enable ENTITY --yes.",
        )
    return Check(
        "entity_settings",
        "PASS",
        f"{len(enabled)}/{len(ENTITY_TYPES)} sensitive-data entities are enabled.",
    )


def _custom_patterns() -> Check | None:
    """A pattern that backtracks would overrun the hook's deadline in the client.

    None when the settings file itself cannot be read: the entity check already
    names that file and its fix.
    """
    from shim_cli.config import load_policy
    from shim_cli.guard.entities import entry_source, unsafe_pattern

    try:
        patterns = load_policy().custom
    except (OSError, ValueError):
        return None
    if not patterns:
        return Check("custom_patterns", "PASS", "No custom patterns are configured.")
    reasons = [
        reason
        for pattern in patterns
        if (reason := unsafe_pattern(pattern.name, entry_source(pattern.entry)))
    ]
    if reasons:
        return Check(
            "custom_patterns",
            "FAIL",
            " ".join(reasons),
            code="CUSTOM_PATTERN_UNSAFE",
            fix="Simplify the pattern, or remove it with "
            "shim config --remove-custom NAME --yes.",
        )
    named = ", ".join(pattern.name for pattern in patterns)
    return Check("custom_patterns", "PASS", f"{len(patterns)} custom: {named}.")


def _run_hook(
    command: list[str], payload: str, environment: dict[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=payload,
        encoding="utf-8",
        capture_output=True,
        timeout=timeout + 5,
        check=False,
        env=environment,
    )


def _runner_check(client: str) -> Check:
    command = [sys.executable, "-I", "-B", "-m", "shim_cli.hook"]
    if client == "claude":
        command.append("claude")
        timeout = claude_settings.HOOK_TIMEOUT_SECONDS
    elif client == "codex":
        timeout = codex_settings.HOOK_TIMEOUT_SECONDS
    elif client == "copilot":
        command.append("copilot")
        timeout = copilot_settings.HOOK_TIMEOUT_SECONDS
    else:
        raise ValueError("unsupported client")
    if client == "copilot":
        safe = json.dumps(
            {
                "prompt": "Synthetic safe prompt",
                "transformedPrompt": "Synthetic safe prompt",
            }
        )
        blocked = json.dumps(
            {
                "prompt": "email demo@example.com",
                "transformedPrompt": "email demo@example.com",
            }
        )
    else:
        safe = json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "Synthetic safe prompt"}
        )
        blocked = json.dumps(
            {"hook_event_name": "UserPromptSubmit", "prompt": "email demo@example.com"}
        )
    try:
        with tempfile.TemporaryDirectory(prefix="shim-doctor-") as directory:
            environment = os.environ.copy()
            environment["SHIM_CONFIG"] = str(Path(directory).resolve() / "config.toml")
            environment["TMPDIR"] = directory
            safe_result = _run_hook(command, safe, environment, timeout)
            block_result = _run_hook(command, blocked, environment, timeout)
        block = json.loads(block_result.stdout)
    except (OSError, UnicodeError, subprocess.SubprocessError, json.JSONDecodeError):
        return Check(
            "runner",
            "FAIL",
            "The local hook runner fixtures did not complete.",
            code="HOOK_RUNNER_FAILED",
            fix=FIX_REINSTALL,
        )
    if safe_result.returncode or safe_result.stdout or safe_result.stderr:
        return Check(
            "runner",
            "FAIL",
            "The local hook runner did not allow the safe fixture silently.",
            code="HOOK_RUNNER_FAILED",
            fix=FIX_REINSTALL,
        )
    if client == "copilot":
        expected, field = "email <EMAIL_1>", "modifiedTransformedPrompt"
    else:
        expected, field = (
            "shim: found EMAIL (1) in your prompt. Not modified.",
            "systemMessage",
        )
    if (
        block_result.returncode
        or block_result.stderr
        or not isinstance(block, dict)
        or block.get(field) != expected
    ):
        return Check(
            "runner",
            "FAIL",
            "The local hook runner did not protect the sensitive fixture.",
            code="HOOK_RUNNER_FAILED",
            fix=FIX_REINSTALL,
        )
    return Check(
        "runner",
        "PASS",
        "Local hook runner allowed and protected direct fixtures correctly.",
    )


def _resolution_check(client: str, installed: frozenset) -> Check:
    resolution = resolve()
    if resolution.source == "none":
        if installed:
            return Check(
                "hook_resolution",
                "PASS",
                f"The installed {client_name(client)} hook runs this package "
                # The name in the fragment, not the alias this process started
                # under, so the reader can match it against the file.
                f"directly ({interpreter_path(sys.executable)}); nothing is "
                "needed on PATH.",
            )
        return Check(
            "hook_resolution",
            "FAIL",
            resolution.detail,
            code="HOOK_RESOLUTION_FAILED",
            fix=f"Run shim install {client}.",
        )
    if resolution.skewed:
        return Check(
            "hook_resolution",
            "WARN",
            f"{resolution.detail} The bundled archive is "
            f"{resolution.archive_version} while the package is "
            f"{resolution.path_version}; the package wins. Update the plugin.",
            code="ARCHIVE_VERSION_SKEW",
            fix="Run claude plugin update shim-cli@shim-cli and restart Claude "
            "Code; in a cloned plugin folder, check out the newest release tag.",
        )
    return Check("hook_resolution", "PASS", resolution.detail)


def _duplicate_check(client: str, installed: frozenset) -> Check:
    if client == "copilot":
        return Check(
            "duplicate_hooks",
            "PASS",
            "The shim plugin stands down in GitHub Copilot CLI, so nothing is "
            "inspected twice.",
        )
    if client != "claude":
        return Check(
            "duplicate_hooks",
            "WARN",
            "Plugin installs are not discoverable for this client; if you "
            "installed both the plugin and `shim install`, remove one.",
            code="PLUGIN_NOT_DISCOVERABLE",
            fix=f"If the {client_name(client)} plugin is installed beside "
            f"shim install {client}, remove the plugin.",
        )
    plugins = installed_plugins()
    if len(plugins) > 1:
        alias = next(p for p in plugins if p["key"].startswith("shim-guard@"))
        return Check(
            "duplicate_hooks",
            "FAIL",
            f"Both the {plugins[0]['key']} and {plugins[1]['key']} plugins are "
            "installed; every event is inspected twice. Run "
            f"`claude plugin uninstall {alias['key']}`.",
            code="DUPLICATE_HOOKS",
            fix=f"claude plugin uninstall {alias['key']}",
        )
    if plugins and installed:
        return Check(
            "duplicate_hooks",
            "FAIL",
            f"Both the {plugins[0]['key']} plugin and a settings hook are installed; "
            "every prompt is inspected twice. Run `shim revert claude` or "
            "uninstall the plugin.",
            code="DUPLICATE_HOOKS",
            fix="Run shim revert claude, or uninstall the plugin.",
        )
    return Check("duplicate_hooks", "PASS", "Exactly one shim hook path is installed.")


_FIX_SPOOL = (
    "Set TMPDIR to a folder you own, or make the shim-session folder in it "
    "yours with mode 700."
)


def _session_record_check() -> Check:
    from shim_cli.session import spool

    try:
        spool.append("shim-doctor-probe", {"probe": True})
        spool.clear("shim-doctor-probe")
    except spool.SpoolError as error:
        return Check(
            "session_record",
            "WARN",
            f"Session records cannot be written ({error}); masking still works "
            "but no session summary will appear.",
            code="SESSION_RECORDS_UNWRITABLE",
            fix=_FIX_SPOOL,
        )
    except OSError:
        return Check(
            "session_record",
            "WARN",
            "Session records cannot be written; masking still works but no "
            "session summary will appear.",
            code="SESSION_RECORDS_UNWRITABLE",
            fix=_FIX_SPOOL,
        )
    return Check(
        "session_record",
        "PASS",
        f"Session records are writable at {spool.root_path()}.",
    )


def _installed_events(client: str, hook_state: Check | None) -> frozenset:
    if client == "copilot":
        passed = hook_state is not None and hook_state.status == "PASS"
        return frozenset({"UserPromptSubmit"}) if passed else frozenset()
    module = claude_settings if client == "claude" else codex_settings
    state = inspect_file(module.target_path(), module.MAX_CONFIG_BYTES)
    if state.kind is not StateKind.FILE or state.content is None:
        return frozenset()
    try:
        return installed_events(state.content, module.hook_groups())
    except ValueError:
        return frozenset()


def _coverage_rows(client: str, installed: frozenset) -> list:
    rows = [
        {
            "event": "UserPromptSubmit",
            "sees": "prompt",
            "can_mask": client == "copilot",
            "can_report": client != "copilot",
            "verified": True,
        }
    ]
    if client == "claude":
        rows.extend(claude_coverage())
        rows.append(
            {
                "event": "Stop",
                "sees": "session record, last_assistant_message",
                "can_mask": False,
                "can_report": True,
                "verified": True,
            }
        )
        rows.append(
            {
                "event": "SessionEnd",
                "sees": "session record",
                "can_mask": False,
                "can_report": False,
                "verified": True,
            }
        )
    return [{**row, "installed": row["event"] in installed} for row in rows]


def _coverage_check(client: str, rows: list) -> Check:
    installed = sum(bool(row["installed"]) for row in rows)
    detail = f"Coverage: {installed} of {len(rows)} events installed"
    if installed == len(rows):
        return Check("coverage", "PASS", f"{detail}.")
    return Check(
        "coverage",
        "WARN",
        f"{detail}; run shim install {client}.",
        code="HOOK_EVENTS_MISSING",
        fix=f"Run shim install {client}.",
    )


def _activation_check(client: str) -> Check:
    if client == "copilot":
        return Check(
            "hook_activation",
            "PASS",
            "GitHub Copilot CLI has no trust step; the hook runs from the next "
            "session.",
        )
    return Check(
        "hook_activation",
        "WARN",
        f"{client_name(client)} hook activation is client UI state; verify shim with /hooks.",
        code="HOOK_ACTIVATION_UNVERIFIED",
        fix=f"Open /hooks in {client_name(client)} and check that shim is listed"
        + (" and enabled." if client == "codex" else "."),
    )


def _print_coverage(client: str, rows: list) -> None:
    table = Table(
        box=box.SIMPLE, pad_edge=False, title=f"{client_name(client)} coverage"
    )
    table.add_column("Event", overflow="fold")
    table.add_column("Sees", overflow="fold")
    table.add_column("Can mask", no_wrap=True)
    table.add_column("Installed", no_wrap=True)
    for row in rows:
        table.add_row(
            str(row["event"]),
            str(row["sees"]),
            "yes" if row["can_mask"] else "no",
            "yes" if row["installed"] else "no",
        )
    console().print(table)


def doctor(*, client: str, as_json: bool) -> None:
    checks = [_version_check(client)]
    if client == "codex":
        checks.append(_codex_hooks_feature())
    legacy_fragment = _has_legacy_fragment(client)
    installed = _installed_events(client, _hook_state(client))
    hook_state = _hook_state(client, legacy_fragment or bool(installed))
    rows = _coverage_rows(client, installed)
    checks.extend(
        check
        for check in (
            hook_state,
            _legacy_state(client, legacy_fragment),
            _entity_settings(),
            _custom_patterns(),
            _session_record_check(),
            _runner_check(client),
            _resolution_check(client, installed),
            _duplicate_check(client, installed),
            _coverage_check(client, rows),
            _activation_check(client),
        )
        if check is not None
    )
    labels = {check.status for check in checks}
    if "FAIL" in labels:
        status = "error"
    elif "WARN" in labels:
        status = "warning"
    else:
        status = "ok"
    if as_json:
        emit_json(
            "doctor",
            status,
            client=client,
            checks=[
                {
                    "name": check.name,
                    "status": check.status,
                    "detail": check.detail,
                    "code": check.code,
                    "fix": check.fix,
                }
                for check in checks
            ],
            coverage=rows,
        )
    else:
        for check in checks:
            emit(check.status, check.detail, error=check.status == "FAIL")
        _print_coverage(client, rows)
    # A warning is not a failure. A healthy install prints one or two — hook
    # activation is state only the client can show, and a client newer than the
    # one tested — so exiting non-zero here made `shim doctor claude && …`
    # useless and taught people to ignore the warnings, which are the only
    # signal that a client changed shape. FAIL keeps exit 2, as every other
    # command in the CLI uses for a refusal.
    if status == "error":
        raise typer.Exit(2)
