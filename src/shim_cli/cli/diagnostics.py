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

from shim_cli.cli.integrations import client_name, client_plan, plan_status
from shim_cli.cli.output import console, emit, emit_json
from shim_cli.cli.resolution import installed_plugins, resolve
from shim_cli.clients.claude import settings as claude_settings
from shim_cli.clients.claude.tool_events import coverage as claude_coverage
from shim_cli.clients.codex import settings as codex_settings
from shim_cli.clients.copilot import settings as copilot_settings


@dataclass(frozen=True)
class Check:
    __slots__ = ("detail", "name", "status")

    name: str
    status: str
    detail: str


def _client_version(
    executable: str, name: str, minimum_text: str, tested_text: str
) -> Check:
    path = shutil.which(executable)
    if path is None:
        return Check(executable, "FAIL", f"{name} executable was not found on PATH.")
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
            executable, "FAIL", f"{name} at {path} could not report its version."
        )
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", result.stdout + result.stderr)
    if result.returncode or match is None:
        return Check(
            executable, "FAIL", f"{name} at {path} has an unrecognized version."
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
        )
    if version > tested:
        return Check(
            executable,
            "WARN",
            f"{name} {version_text} is newer than tested {tested_text}.",
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


def _codex_hooks_feature() -> Check:
    path = shutil.which("codex")
    if path is None:
        return Check("hooks_feature", "FAIL", "Codex executable was not found on PATH.")
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
            "hooks_feature", "FAIL", "Codex hook support could not be checked."
        )
    enabled = any(
        fields and fields[0] == "hooks" and fields[-1] == "true"
        for fields in map(str.split, result.stdout.splitlines())
    )
    if result.returncode or not enabled:
        return Check("hooks_feature", "FAIL", "Codex hook support is not enabled.")
    return Check("hooks_feature", "PASS", "Codex hook support is enabled.")


def _legacy_state(client: str) -> Check:
    """R7: name every 0.2.0 shape that is still on disk. Changes nothing."""
    from shim_cli.clients.copilot import settings as copilot_settings
    from shim_cli.config import legacy_config_path
    from shim_cli.session import ledger
    from shim_cli.settings_files import StateKind, inspect_file

    found: list[str] = []
    if client == "copilot":
        legacy = copilot_settings.legacy_target_path()
        state = inspect_file(legacy, copilot_settings.MAX_CONFIG_BYTES)
        if state.kind is StateKind.FILE and state.content is not None:
            if copilot_settings.is_ours(state.content):
                found.append(f"hook file uses the old name at {legacy}")
    if _has_legacy_fragment(client):
        found.append("the installed hook fragment names the old module")
    settings_file = legacy_config_path()
    if settings_file is not None and settings_file.is_file():
        found.append(f"settings are still at {settings_file}")
    try:
        directory = ledger.legacy_root_path()
    except ledger.LedgerError:
        directory = None
    if directory is not None and any(
        directory.glob(f"{ledger.FILE_PREFIX}*{ledger.FILE_SUFFIX}")
    ):
        found.append(f"ledger files are still in {directory}")
    if not found:
        return Check("legacy_names", "PASS", "No 0.2.0 names are left on disk.")
    return Check(
        "legacy_names",
        "WARN",
        "; ".join(found) + f"; run shim install {client}",
    )


def _has_legacy_fragment(client: str) -> bool:
    """Claude and Codex carry the fragment in their own settings document."""
    from shim_cli.clients.claude import settings as claude_settings
    from shim_cli.clients.codex import settings as codex_settings
    from shim_cli.clients.hook_settings import remove_groups
    from shim_cli.settings_files import StateKind, inspect_file

    if client == "claude":
        module = claude_settings
    elif client == "codex":
        module = codex_settings
    else:
        return False
    state = inspect_file(module.target_path(), module.MAX_CONFIG_BYTES)
    if state.kind is not StateKind.FILE or state.content is None:
        return False
    try:
        return (
            remove_groups(state.content, module.legacy_hook_groups()) != state.content
        )
    except ValueError:
        return False


def _hook_state(client: str) -> Check:
    name = client_name(client)
    try:
        label, state = plan_status(client_plan(client, "install"))
    except (OSError, ValueError):
        return Check(
            "hook_configuration",
            "FAIL",
            f"Could not inspect {name} hook configuration.",
        )
    messages = {
        "installed": f"shim's exact {name} hook group is present.",
        "not_installed": f"shim's {name} hook group is not installed.",
        "conflict": f"{name} hook configuration needs manual review.",
        "unsafe": f"{name} hook configuration cannot be trusted safely.",
    }
    return Check("hook_configuration", label, messages[state])


def _entity_settings() -> Check:
    from shim_cli.config import load_entities
    from shim_cli.guard import ENTITY_TYPES

    try:
        enabled = load_entities()
    except (OSError, ValueError):
        return Check(
            "entity_settings",
            "FAIL",
            "Entity settings are unsafe or invalid; reset malformed contents or review the path.",
        )
    if not enabled:
        return Check(
            "entity_settings",
            "WARN",
            "All sensitive-data detection is disabled; review with `shim config`.",
        )
    return Check(
        "entity_settings",
        "PASS",
        f"{len(enabled)}/{len(ENTITY_TYPES)} sensitive-data entities are enabled.",
    )


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
            environment["SHIM_GUARD_CONFIG"] = str(
                Path(directory).resolve() / "config.toml"
            )
            environment["TMPDIR"] = directory
            safe_result = _run_hook(command, safe, environment, timeout)
            block_result = _run_hook(command, blocked, environment, timeout)
        block = json.loads(block_result.stdout)
    except (OSError, UnicodeError, subprocess.SubprocessError, json.JSONDecodeError):
        return Check(
            "runner", "FAIL", "The local hook runner fixtures did not complete."
        )
    if safe_result.returncode or safe_result.stdout or safe_result.stderr:
        return Check(
            "runner",
            "FAIL",
            "The local hook runner did not allow the safe fixture silently.",
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
        )
    return Check(
        "runner",
        "PASS",
        "Local hook runner allowed and protected direct fixtures correctly.",
    )


def _resolution_check() -> Check:
    resolution = resolve()
    if resolution.source == "none":
        return Check("hook_resolution", "FAIL", resolution.detail)
    if resolution.skewed:
        return Check(
            "hook_resolution",
            "WARN",
            f"{resolution.detail} The bundled archive is "
            f"{resolution.archive_version} while the package is "
            f"{resolution.path_version}; the package wins. Update the plugin.",
        )
    return Check("hook_resolution", "PASS", resolution.detail)


def _duplicate_check(client: str) -> Check:
    if client != "claude":
        return Check(
            "duplicate_hooks",
            "WARN",
            "Plugin installs are not discoverable for this client; if you "
            "installed both the plugin and `shim install`, remove one.",
        )
    plugins = installed_plugins()
    try:
        _label, state = plan_status(client_plan(client, "install"))
    except (OSError, ValueError):
        return Check(
            "duplicate_hooks", "WARN", "The client hook settings could not be read."
        )
    if len(plugins) > 1:
        alias = next(p for p in plugins if p["key"].startswith("shim-guard@"))
        return Check(
            "duplicate_hooks",
            "FAIL",
            f"Both the {plugins[0]['key']} and {plugins[1]['key']} plugins are "
            "installed; every event is inspected twice. Run "
            f"`claude plugin uninstall {alias['key']}`.",
        )
    if plugins and state == "installed":
        return Check(
            "duplicate_hooks",
            "FAIL",
            f"Both the {plugins[0]['key']} plugin and a settings hook are installed; "
            "every prompt is inspected twice. Run `shim revert claude` or "
            "uninstall the plugin.",
        )
    return Check("duplicate_hooks", "PASS", "Exactly one SHIM hook path is installed.")


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
        )
    except OSError:
        return Check(
            "session_record",
            "WARN",
            "Session records cannot be written; masking still works but no "
            "session summary will appear.",
        )
    return Check(
        "session_record",
        "PASS",
        f"Session records are writable at {spool.root_path()}.",
    )


def _coverage_rows(client: str) -> list:
    rows = [
        {
            "event": "UserPromptSubmit",
            "sees": "prompt",
            "can_mask": client == "copilot",
            "can_report": client != "copilot",
            "verified": True,
            "installed": True,
        }
    ]
    if client == "claude":
        rows.extend(dict(row) for row in claude_coverage())
        rows.append(
            {
                "event": "Stop",
                "sees": "session record",
                "can_mask": False,
                "can_report": True,
                "verified": True,
                "installed": True,
            }
        )
        rows.append(
            {
                "event": "SessionEnd",
                "sees": "session record",
                "can_mask": False,
                "can_report": False,
                "verified": True,
                "installed": True,
            }
        )
    return rows


def _coverage_check(client: str) -> Check:
    rows = _coverage_rows(client)
    installed = sum(bool(row["installed"]) for row in rows)
    detail = f"Coverage: {installed} of {len(rows)} events installed."
    return Check("coverage", "PASS", detail)


def _activation_check(client: str) -> Check:
    return Check(
        "hook_activation",
        "WARN",
        f"{client_name(client)} hook activation is client UI state; verify SHIM with /hooks.",
    )


def _print_coverage(client: str) -> None:
    table = Table(
        box=box.SIMPLE, pad_edge=False, title=f"{client_name(client)} coverage"
    )
    table.add_column("Event", overflow="fold")
    table.add_column("Sees", overflow="fold")
    table.add_column("Can mask", no_wrap=True)
    table.add_column("Installed", no_wrap=True)
    for row in _coverage_rows(client):
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
    checks.extend(
        (
            _hook_state(client),
            _legacy_state(client),
            _entity_settings(),
            _session_record_check(),
            _runner_check(client),
            _resolution_check(),
            _duplicate_check(client),
            _coverage_check(client),
            _activation_check(client),
        )
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
            checks=[{"name": check.name, "status": check.status} for check in checks],
            coverage=_coverage_rows(client),
        )
    else:
        for check in checks:
            emit(check.status, check.detail, error=check.status == "FAIL")
        _print_coverage(client)
    if status == "error":
        raise typer.Exit(2)
    if status == "warning":
        raise typer.Exit(1)
