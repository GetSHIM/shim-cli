"""install, status and doctor run the exact command the client will run.

Every hook runs `<python> -I -B -m shim_cli.hook`, and -I ignores the user site
and PYTHONPATH. A `pip install --user` shim imports in the CLI and not in the
hook, so the hook fails open on every event. The installation is reproduced
for real: a fresh environment with nothing installed, reaching shim only
through PYTHONPATH, which -I drops exactly as it drops the user site.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shim_cli.cli import diagnostics
from shim_cli.cli.app import app
from shim_cli.clients.claude import settings as claude
from shim_cli.clients.codex import settings as codex
from shim_cli.clients.copilot import settings as copilot

MODULES = {"claude": claude, "codex": codex, "copilot": copilot}
CLIENTS = pytest.mark.parametrize("client", sorted(MODULES))
runner = CliRunner()


@pytest.fixture(scope="module")
def outside_python(tmp_path_factory) -> Path:
    """A Python that imports shim only while PYTHONPATH is honoured."""
    root = tmp_path_factory.mktemp("outside") / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", root], check=True)
    return root / "bin" / "python"


def _shim(python: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    reachable = os.pathsep.join(p for p in sys.path if p and Path(p).is_dir())
    return subprocess.run(
        [python, "-c", "from shim_cli.cli.app import main; main()", *argv],
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        check=False,
        # No shim-hook on PATH, so doctor's resolution check reads the fragment.
        env={**os.environ, "PYTHONPATH": reachable, "PATH": "/usr/bin:/bin"},
    )


def _install_as_1_2_1(client: str, python: Path) -> Path:
    """The fragment 1.2.1 wrote for this Python without checking that it runs."""
    target = MODULES[client].target_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(MODULES[client].add_hook(None, python))
    return target


@CLIENTS
def test_install_refuses_a_hook_that_cannot_import_shim(
    client: str, outside_python: Path
) -> None:
    target = MODULES[client].target_path()

    result = _shim(outside_python, "install", client, "--yes", "--json")

    assert result.returncode == 2, result.stdout
    payload = json.loads(result.stdout)
    assert payload["code"] == "DETECTOR_UNAVAILABLE"
    assert f"{outside_python} -I -B -m shim_cli.hook" in payload["error"]
    assert "uv tool install" in payload["fix"] and "pipx install" in payload["fix"]
    assert f"shim install {client}" in payload["fix"]
    assert not target.exists()
    assert not target.parent.exists(), "install wrote nothing, not even a folder"


@CLIENTS
def test_status_fails_a_hook_that_cannot_import_shim(
    client: str, outside_python: Path
) -> None:
    _install_as_1_2_1(client, outside_python)

    result = _shim(outside_python, "status", client, "--json")

    assert result.returncode == 2, result.stdout
    payload = json.loads(result.stdout)
    assert payload["code"] == "DETECTOR_UNAVAILABLE"
    assert "pipx install" in payload["fix"]


def test_install_over_that_hook_is_not_already_installed(outside_python: Path) -> None:
    target = _install_as_1_2_1("claude", outside_python)
    before = target.read_bytes()

    result = _shim(outside_python, "install", "claude", "--yes")

    assert result.returncode == 2
    assert "already installed" not in result.stdout + result.stderr
    assert target.read_bytes() == before


def test_doctor_does_not_pass_a_hook_that_cannot_import_shim(
    outside_python: Path,
) -> None:
    _install_as_1_2_1("claude", outside_python)

    result = _shim(outside_python, "doctor", "claude", "--json")

    assert result.returncode == 2
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["runner"]["status"] == "FAIL"
    assert checks["runner"]["code"] == "HOOK_RUNNER_FAILED"
    assert (
        f"{outside_python} -I -B -m shim_cli.hook claude" in checks["runner"]["detail"]
    )
    # Not "PASS The installed Claude Code hook runs this package directly".
    assert checks["hook_resolution"]["status"] == "FAIL"
    assert checks["hook_resolution"]["code"] == "HOOK_RESOLUTION_FAILED"


@CLIENTS
def test_install_runs_the_command_it_writes(client: str, monkeypatch) -> None:
    """A healthy hook installs, and the command checked is the one on disk."""
    ran = []
    real = diagnostics._run_hook

    def record(command, *args):
        ran.append(command)
        return real(command, *args)

    monkeypatch.setattr(diagnostics, "_run_hook", record)

    result = runner.invoke(app, ["install", client, "--yes"])

    assert result.exit_code == 0, result.output
    written = json.loads(MODULES[client].target_path().read_bytes())
    handlers = [
        handler
        for entries in written["hooks"].values()
        for entry in entries
        for handler in entry.get("hooks", [entry])
    ]
    on_disk = {
        tuple([handler["command"], *handler["args"]])
        if "args" in handler
        else tuple(shlex.split(handler["command"]))
        for handler in handlers
    }
    assert len(on_disk) == 1
    assert {tuple(command) for command in ran} == on_disk
