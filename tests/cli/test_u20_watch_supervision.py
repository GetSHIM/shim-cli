"""`shim watch` keeps its proxy for as long as the client lives, passes signals
on once, and reports how the client ended. Real processes, real signals."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from shim_cli.cli.app import app
from shim_cli.watch import proxy

# A stand-in for `claude`: records what reached it into a log, one line each.
# `listener` is whether the proxy still accepted a connection when it ended.
CLIENT = """\
import os, signal, socket, sys, time, urllib.parse

log = {log!r}
def note(line):
    with open(log, "a") as handle:
        handle.write(line + "\\n")

def listener():
    address = urllib.parse.urlsplit(os.environ["ANTHROPIC_BASE_URL"])
    try:
        socket.create_connection((address.hostname, address.port), timeout=1).close()
        return "up"
    except OSError:
        return "down"

def finish(code):
    note("listener " + listener())
    sys.exit(code)

{body}
note("ready")
while True:
    time.sleep(0.05)
"""


def _client(tmp_path: Path, body: str) -> tuple[Path, Path]:
    log = tmp_path / "client.log"
    program = tmp_path / "claude"
    program.write_text(
        f"#!{sys.executable}\n"
        + CLIENT.format(log=str(log), body=textwrap.dedent(body))
    )
    program.chmod(0o755)
    return program, log


def _lines(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


def _wait_for(
    log: Path, line: str, shim: subprocess.Popen, seconds: float = 20
) -> None:
    deadline = time.monotonic() + seconds
    while line not in _lines(log):
        if time.monotonic() > deadline:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(shim.pid, signal.SIGKILL)
            stdout, stderr = shim.communicate()
            pytest.fail(
                f"{line!r} never logged: {_lines(log)}; shim watch exited "
                f"{shim.returncode}\nstdout: {stdout!r}\nstderr: {stderr!r}"
            )
        time.sleep(0.05)


def _watch(tmp_path: Path, program: Path) -> subprocess.Popen:
    environment = {
        key: value for key, value in os.environ.items() if key != "ANTHROPIC_BASE_URL"
    }
    environment["HOME"] = str(tmp_path / "home")
    environment["TMPDIR"] = str(tmp_path)
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            "from shim_cli.cli.app import main; main()",
            "watch",
            "--",
            str(program),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _finish(shim: subprocess.Popen) -> int:
    try:
        return shim.wait(timeout=30)
    finally:
        if shim.poll() is None:
            os.killpg(shim.pid, signal.SIGKILL)
        shim.communicate()


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_a_terminated_watch_hands_the_signal_to_the_client_and_waits_for_it(
    tmp_path, number
):
    program, log = _client(
        tmp_path,
        f"""
        def stop(number, frame):
            note("signal " + str(number))
            time.sleep(0.3)
            finish(7)
        signal.signal(signal.{number.name}, stop)
        """,
    )
    shim = _watch(tmp_path, program)
    _wait_for(log, "ready", shim)

    os.kill(shim.pid, number)

    assert _finish(shim) == 7
    assert _lines(log) == ["ready", f"signal {int(number)}", "listener up"]


def test_one_ctrl_c_reaches_the_client_once(tmp_path):
    program, log = _client(
        tmp_path,
        """
        def interrupted(number, frame):
            note("interrupt")
            time.sleep(0.5)
            finish(0)
        signal.signal(signal.SIGINT, interrupted)
        """,
    )
    shim = _watch(tmp_path, program)
    _wait_for(log, "ready", shim)

    os.killpg(shim.pid, signal.SIGINT)  # what a terminal does on Ctrl-C

    assert _finish(shim) == 0
    assert _lines(log) == ["ready", "interrupt", "listener up"]


def test_repeated_ctrl_c_escalates_and_the_proxy_stays_until_the_client_exits(
    tmp_path,
):
    program, log = _client(
        tmp_path,
        """
        def stubborn(number, frame):
            note("interrupt")
        def terminated(number, frame):
            time.sleep(0.3)
            finish(9)
        signal.signal(signal.SIGINT, stubborn)
        signal.signal(signal.SIGTERM, terminated)
        """,
    )
    shim = _watch(tmp_path, program)
    _wait_for(log, "ready", shim)

    os.killpg(shim.pid, signal.SIGINT)
    _wait_for(log, "interrupt", shim)
    time.sleep(0.6)  # signals do not queue: let the first one be handled
    os.killpg(shim.pid, signal.SIGINT)

    assert _finish(shim) == 9
    assert _lines(log)[-1] == "listener up"


def test_ctrl_c_is_forwarded_when_the_client_is_in_another_process_group(tmp_path):
    program, log = _client(
        tmp_path,
        """
        os.setpgrp()
        def interrupted(number, frame):
            note("interrupt")
            finish(0)
        signal.signal(signal.SIGINT, interrupted)
        """,
    )
    shim = _watch(tmp_path, program)
    _wait_for(log, "ready", shim)

    os.kill(shim.pid, signal.SIGINT)

    assert _finish(shim) == 0
    assert _lines(log) == ["ready", "interrupt", "listener up"]


def _signalled(monkeypatch, number: int):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: "/synthetic/claude")
    running = SimpleNamespace(
        base_url="http://127.0.0.1:1234", session=proxy.Session(), stop=Mock()
    )
    monkeypatch.setattr(proxy, "start", lambda *_: running)
    monkeypatch.setattr(
        "subprocess.Popen", Mock(return_value=SimpleNamespace(wait=lambda: -number))
    )


def test_a_client_killed_by_a_signal_exits_as_a_shell_would(monkeypatch):
    _signalled(monkeypatch, signal.SIGSEGV)

    result = CliRunner().invoke(app, ["watch", "--", "claude"])

    assert result.exit_code == 128 + signal.SIGSEGV
    assert "SIGSEGV" in result.output


def test_the_json_report_of_a_killed_client_is_not_ok(monkeypatch):
    _signalled(monkeypatch, signal.SIGSEGV)

    result = CliRunner().invoke(app, ["watch", "--json", "--", "claude"])

    document = json.loads(result.stdout)
    assert result.exit_code == 128 + signal.SIGSEGV
    assert document["exit_code"] == 128 + signal.SIGSEGV
    assert document["status"] == "signalled"
    assert document["signal"] == "SIGSEGV"


@pytest.mark.parametrize("as_json", [False, True])
def test_the_refusal_for_a_configured_endpoint_does_not_advise_unsetting_it(
    monkeypatch, as_json
):
    """A gateway's credential travels with its URL; the proxy only reaches
    api.anthropic.com, so unsetting just the URL would send that credential
    there."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.invalid")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-gateway-token-0123456789")
    monkeypatch.setattr("shutil.which", lambda _: "/synthetic/claude")
    monkeypatch.setenv("COLUMNS", "1000")
    start = Mock()
    monkeypatch.setattr(proxy, "start", start)

    result = CliRunner().invoke(
        app, ["watch", *(["--json"] if as_json else []), "--", "claude"]
    )

    assert result.exit_code == 2
    text = result.output.lower()
    assert "unset" not in text
    assert "api.anthropic.com" in text
    assert "credential" in text
    assert "synthetic-gateway-token" not in text
    assert "gateway.invalid" not in text
    start.assert_not_called()
