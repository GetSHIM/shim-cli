"""install, revert, status and doctor over a file another shim installation wrote.

The other installation is simulated the way it really arrives: a settings file
whose hooks name another interpreter. Every client file lives under tmp_path.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shim_cli.cli.app import app
from shim_cli.cli.integrations import client_name
from shim_cli.clients.claude import settings as claude
from shim_cli.clients.codex import settings as codex
from shim_cli.clients.copilot import settings as copilot
from shim_cli.clients.hook_settings import interpreter_path

runner = CliRunner()
MODULES = {"claude": claude, "codex": codex}
FOREIGN = {"hooks": [{"type": "command", "command": "existing-audit-hook"}]}
SETTINGS_CLIENTS = pytest.mark.parametrize("client", sorted(MODULES))
VERSIONS = {
    "claude": "2.1.210 (Claude Code)",
    "codex": "codex 0.149.0",
    "copilot": "GitHub Copilot CLI 1.0.80.",
}


def _client(monkeypatch, tmp_path: Path, client: str) -> None:
    """A home for the client and a stand-in executable doctor can ask."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    executable = tmp_path / "bin" / client
    executable.parent.mkdir()
    executable.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = features ]; then printf "hooks stable true\\n"; exit 0; fi\n'
        f"printf '{VERSIONS[client]}\\n'\n"
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(executable.parent), str(Path(sys.executable).parent))),
    )


def _other(tmp_path: Path, *, exists: bool = False) -> Path:
    """The interpreter of another shim installation, e.g. pipx beside uv tool."""
    other = tmp_path / "other-install" / "bin" / "python"
    if exists:
        other.parent.mkdir(parents=True)
        other.write_text("#!/bin/sh\n")
        other.chmod(0o700)
    return other


def _write(target: Path, document: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2) + "\n")


def _both_sets(client: str, other: Path) -> dict:
    """What 1.2.1 left after `shim install` from two installations."""
    module = MODULES[client]
    document = {"hooks": {module.PROMPT_EVENT: [FOREIGN]}}
    for interpreter in (other, sys.executable):
        for event, group in module.hook_groups(interpreter):
            document["hooks"].setdefault(event, []).append(group)
    return document


def _doctor(client: str) -> tuple[int, dict]:
    code, checks, _ = _doctor_report(client)
    return code, checks


def _doctor_report(client: str) -> tuple[int, dict, dict]:
    """Exit code, checks by name, and findings by rule id."""
    result = runner.invoke(app, ["doctor", client, "--json"])
    document = json.loads(result.stdout)
    return (
        result.exit_code,
        {check["name"]: check for check in document["checks"]},
        {finding["rule_id"]: finding for finding in document["findings"]},
    )


@SETTINGS_CLIENTS
def test_install_replaces_another_installation_s_hooks(
    client: str, monkeypatch, tmp_path: Path
) -> None:
    _client(monkeypatch, tmp_path, client)
    module, other = MODULES[client], _other(tmp_path)
    target = module.target_path()
    original = {"model": "kept", "hooks": {module.PROMPT_EVENT: [FOREIGN]}}
    target.parent.mkdir(parents=True)
    target.write_bytes(module.add_hook(json.dumps(original).encode(), other))

    installed = runner.invoke(app, ["install", client, "--yes"])
    document = json.loads(target.read_bytes())

    assert installed.exit_code == 0, installed.output
    assert str(other) in "".join(installed.output.split())
    assert str(other) not in target.read_text()
    assert document["hooks"][module.PROMPT_EVENT][0] == FOREIGN
    for event, group in module.hook_groups():
        assert document["hooks"][event].count(group) == 1
        assert document["hooks"][event][-1] == group
    status = runner.invoke(app, ["status", client, "--json"])
    assert json.loads(status.stdout)["state"] == "installed"


@SETTINGS_CLIENTS
def test_revert_removes_every_installation_s_hooks_and_says_so_truthfully(
    client: str, monkeypatch, tmp_path: Path
) -> None:
    _client(monkeypatch, tmp_path, client)
    target = MODULES[client].target_path()
    _write(target, _both_sets(client, _other(tmp_path)))

    reverted = runner.invoke(app, ["revert", client, "--yes"])
    status = runner.invoke(app, ["status", client, "--json"])

    assert reverted.exit_code == 0, reverted.output
    assert "PASS Removed shim" in reverted.output
    assert json.loads(target.read_bytes()) == {
        "hooks": {MODULES[client].PROMPT_EVENT: [FOREIGN]}
    }
    assert json.loads(status.stdout)["state"] == "not_installed"
    assert runner.invoke(app, ["revert", client, "--yes"]).output.startswith(
        "PASS shim is not installed"
    )


@SETTINGS_CLIENTS
def test_doctor_names_a_second_installation_s_hooks_and_the_fix(
    client: str, monkeypatch, tmp_path: Path
) -> None:
    _client(monkeypatch, tmp_path, client)
    other = _other(tmp_path, exists=True)
    _write(MODULES[client].target_path(), _both_sets(client, other))

    code, checks, findings = _doctor_report(client)

    duplicate = checks["duplicate_hooks"]
    assert code == 2
    assert duplicate["status"] == "FAIL"
    assert duplicate["code"] == "DUPLICATE_HOOKS"
    assert duplicate["fix"] == f"Run shim install {client}."
    assert str(other) in duplicate["detail"]
    assert str(interpreter_path(sys.executable)) in duplicate["detail"]
    assert "Exactly one" not in json.dumps(checks)
    # The finding an agent acts on names the same fix, not a revert that
    # would remove shim from both installations.
    remediation = findings["cli.doctor.duplicate_hooks"]["remediation"]["text"]["en"]
    assert f"shim install {client} --yes leaves a single settings hook" in remediation

    assert runner.invoke(app, ["install", client, "--yes"]).exit_code == 0
    after = _doctor(client)[1]["duplicate_hooks"]
    assert (after["status"], after["code"]) == {
        "claude": ("PASS", None),
        "codex": ("WARN", "PLUGIN_NOT_DISCOVERABLE"),
    }[client]


@pytest.mark.parametrize("client", ("claude", "codex", "copilot"))
@pytest.mark.parametrize("exists", (True, False), ids=("other runs", "other gone"))
def test_doctor_names_the_installation_the_hook_runs_when_it_is_not_this_one(
    client: str, exists: bool, monkeypatch, tmp_path: Path
) -> None:
    _client(monkeypatch, tmp_path, client)
    other = _other(tmp_path, exists=exists)
    if client == "copilot":
        target = copilot.target_path()
        target.parent.mkdir(parents=True)
        target.write_bytes(copilot.add_hook(None, other))
    else:
        target = MODULES[client].target_path()
        target.parent.mkdir(parents=True)
        target.write_bytes(MODULES[client].add_hook(None, other))

    code, checks, findings = _doctor_report(client)

    state = checks["hook_configuration"]
    assert state["status"] == ("WARN" if exists else "FAIL")
    assert state["code"] == "HOOK_NOT_INSTALLED"
    assert state["fix"] == f"Run shim install {client}."
    assert str(other) in state["detail"]
    assert ("so nothing is inspected" in state["detail"]) is not exists
    assert "No shim hook is installed" not in checks["duplicate_hooks"]["detail"]
    assert code == (0 if exists else 2)
    # Another installation's hook may be inspecting, so the finding must not
    # say that nothing is.
    assert findings["cli.doctor.hook_not_installed"]["summary"]["en"] == (
        f"The {client_name(client)} settings file does not hold exactly "
        "this shim installation's hook; without any shim hook there, nothing is "
        "inspected."
    )


def test_doctor_does_not_say_nothing_is_inspected_while_this_installation_runs(
    monkeypatch, tmp_path: Path
) -> None:
    """Copilot's file holds both installations' hooks, and the other one is
    gone: its hook fails, this one's still inspects."""
    _client(monkeypatch, tmp_path, "copilot")
    other = _other(tmp_path)
    document = copilot.hook_document(other)
    hooks = document["hooks"]["userPromptTransformed"]
    hooks.extend(copilot.hook_document()["hooks"]["userPromptTransformed"])
    _write(copilot.target_path(), document)

    state = _doctor("copilot")[1]["hook_configuration"]

    assert (state["status"], state["code"]) == ("FAIL", "HOOK_NOT_INSTALLED")
    assert f"{other} cannot be run" in state["detail"]
    assert "nothing is inspected" not in state["detail"]


@pytest.mark.parametrize("both", (False, True), ids=("other only", "both sets"))
@SETTINGS_CLIENTS
def test_status_names_the_other_installation(
    client: str, both: bool, monkeypatch, tmp_path: Path
) -> None:
    _client(monkeypatch, tmp_path, client)
    other = _other(tmp_path)
    module = MODULES[client]
    if both:
        _write(module.target_path(), _both_sets(client, other))
    else:
        _write(module.target_path(), json.loads(module.add_hook(None, other)))

    result = runner.invoke(app, ["status", client])

    assert result.exit_code == 1
    assert "".join(result.output.split()) == "".join(
        f"WARN {client_name(client)} runs shim's hook from another installation "
        f"({other}); run shim install {client} to run only this one.".split()
    )


SHIM_THEN = (
    "/opt/py/bin/python -I -B -m shim_cli.hook | tee -a /tmp/shim-debug.log",
    "/opt/py/bin/python -I -B -m shim_cli.hook && /usr/local/bin/notify-done",
)


@SETTINGS_CLIENTS
def test_install_and_revert_keep_a_hook_that_runs_shim_and_something_else(
    client: str, monkeypatch, tmp_path: Path
) -> None:
    """A user's own hook that wraps shim's command is theirs, not shim's."""
    _client(monkeypatch, tmp_path, client)
    module = MODULES[client]
    original = {
        "hooks": {
            module.PROMPT_EVENT: [
                {"hooks": [{"type": "command", "command": command, "timeout": 30}]}
                for command in SHIM_THEN
            ]
        }
    }
    _write(module.target_path(), original)
    before = module.target_path().read_bytes()

    untouched = runner.invoke(app, ["revert", client, "--yes"])

    assert (
        untouched.output == f"PASS shim is not installed for {client_name(client)}.\n"
    )
    assert module.target_path().read_bytes() == before

    installed = runner.invoke(app, ["install", client, "--yes"])
    groups = json.loads(module.target_path().read_bytes())["hooks"][module.PROMPT_EVENT]

    assert installed.exit_code == 0, installed.output
    assert "another installation" not in installed.output
    assert groups == [*original["hooks"][module.PROMPT_EVENT], module.hook_group()]

    assert runner.invoke(app, ["revert", client, "--yes"]).exit_code == 0
    assert json.loads(module.target_path().read_bytes()) == original


def test_copilot_install_and_revert_take_back_a_file_another_installation_wrote(
    monkeypatch, tmp_path: Path
) -> None:
    _client(monkeypatch, tmp_path, "copilot")
    other = _other(tmp_path)
    target = copilot.target_path()
    target.parent.mkdir(parents=True)
    target.write_bytes(copilot.add_hook(None, other))

    status = runner.invoke(app, ["status", "copilot"])

    assert status.exit_code == 1
    assert f"anotherinstallation({other})" in "".join(status.output.split())

    installed = runner.invoke(app, ["install", "copilot", "--yes"])

    assert installed.exit_code == 0, installed.output
    assert json.loads(target.read_bytes()) == copilot.hook_document()

    target.write_bytes(copilot.add_hook(None, other))
    reverted = runner.invoke(app, ["revert", "copilot", "--yes"])

    assert reverted.exit_code == 0, reverted.output
    assert not target.exists()
