from __future__ import annotations

import datetime
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

import typer

from shim_cli.cli.output import emit, emit_error, emit_json, terminal_text

# Copilot proxying requires BYOK.
BASE_URL_VARIABLES = {"claude": "ANTHROPIC_BASE_URL"}
UPSTREAMS = {"claude": "api.anthropic.com"}
GRACE_SECONDS = 10

# Measured on Codex 0.151.0, 8 September 2026: the base URL comes from Codex's
# own configuration and `OPENAI_BASE_URL` reaches nothing, so starting a proxy
# and setting the variable ran the whole session past it and reported nothing
# measured, while exiting 0. Refusing is honest; PRD-13 makes it work.
# docs/probe-2026-09-codex-watch.md
REFUSED = {
    "codex": (
        "shim watch does not support codex. Codex takes its endpoint from its "
        "own configuration, so the proxy would be bypassed and the session "
        "measured as empty. The Codex prompt hook is unaffected."
    )
}


def _supervise(process: subprocess.Popen) -> int:
    """Wait for the client while passing signals on. The proxy outlives the
    client: until it exits, another local process could take the port and
    receive its credentials. Each further request escalates: the signal, then
    terminate, then kill; the first also arms a kill after GRACE_SECONDS."""
    requests = 0
    killer: threading.Timer | None = None

    def escalate(number: int) -> None:
        nonlocal requests, killer
        requests += 1
        if requests == 1:
            # At a terminal the whole foreground group already got Ctrl-C.
            if number != signal.SIGINT or os.getpgid(process.pid) != os.getpgrp():
                process.send_signal(number)
            killer = threading.Timer(GRACE_SECONDS, process.kill)
            killer.daemon = True
            killer.start()
        elif requests == 2:
            process.terminate()
        else:
            process.kill()

    previous = {
        number: signal.signal(number, lambda n, _frame: escalate(n))
        for number in (signal.SIGTERM, signal.SIGHUP)
    }
    try:
        while True:
            try:
                return process.wait()
            except KeyboardInterrupt:
                escalate(signal.SIGINT)
    finally:
        if killer is not None:
            killer.cancel()
        for number, handler in previous.items():
            signal.signal(number, handler)


def watch(*, command: tuple, as_json: bool) -> None:
    if not command:
        emit_error(
            "watch",
            "NOTHING_TO_RUN",
            "Nothing to run. Try: shim watch -- claude",
            "shim watch -- claude",
            as_json=as_json,
        )
    client = os.path.basename(command[0])
    variable = BASE_URL_VARIABLES.get(client, "")
    if not variable:
        emit_error(
            "watch",
            "CLIENT_UNSUPPORTED",
            REFUSED.get(client)
            or f"shim watch does not support {client}. Supported: "
            + ", ".join(sorted(BASE_URL_VARIABLES)),
            f"shim install {client} installs the hook, which needs no proxy."
            if client in ("codex", "copilot")
            else "shim watch -- claude",
            as_json=as_json,
        )
    if os.environ.get(variable):
        emit_error(
            "watch",
            "BASE_URL_ALREADY_SET",
            f"{variable} is already configured, so {client} sends its requests and "
            f"its credential to that endpoint. The proxy only reaches "
            f"{UPSTREAMS[client]}, which would receive the other endpoint's "
            f"credential instead. Use the hook (shim install {client}), which "
            f"does not need the proxy.",
            f"shim install {client}",
            as_json=as_json,
        )
    if shutil.which(command[0]) is None and not os.path.exists(command[0]):
        emit_error(
            "watch",
            "EXECUTABLE_NOT_FOUND",
            f"{command[0]} was not found on PATH.",
            None,
            as_json=as_json,
        )

    from shim_cli.cli.configuration import settings_check
    from shim_cli.config import load_policy
    from shim_cli.guard import evaluate
    from shim_cli.watch import proxy, report

    try:
        policy = load_policy()
    except ValueError as error:
        problem = settings_check(error)
        assert problem.code is not None
        emit_error(
            "watch",
            problem.code,
            problem.detail.rstrip("."),
            problem.fix,
            as_json=as_json,
        )

    try:
        running = proxy.start(
            UPSTREAMS[client],
            lambda text: evaluate(text, policy.entities, policy.custom),
        )
    except OSError as error:
        emit_error(
            "watch",
            "PROXY_FAILED",
            f"The proxy could not start ({error}); nothing was run.",
            None,
            as_json=as_json,
        )

    if not as_json:
        emit("PASS", f"Watching {client} on {running.base_url}. Nothing is modified.")

    environment = dict(os.environ)
    environment[variable] = running.base_url
    started = time.monotonic()
    began = datetime.datetime.now(datetime.timezone.utc)
    try:
        process = subprocess.Popen(command, env=environment)
        code = _supervise(process)
    except OSError as error:
        emit_error(
            "watch",
            "CLIENT_START_FAILED",
            f"{command[0]} could not be started ({error}).",
            None,
            as_json=as_json,
        )
    finally:
        running.stop()

    elapsed = time.monotonic() - started
    signalled = ""
    if code < 0:
        try:
            signalled = signal.Signals(-code).name
        except ValueError:
            signalled = f"signal {-code}"
        code = 128 - code
        if not as_json:
            emit("WARN", f"{client} was ended by {signalled}.", error=True)
    if as_json:
        from shim_cli.cli.findings import from_watch

        document = report.as_json(running.session, elapsed)
        ended = datetime.datetime.now(datetime.timezone.utc)
        emit_json(
            "watch",
            "signalled" if signalled else "ok",
            exit_code=code,
            **({"signal": signalled} if signalled else {}),
            findings=from_watch(document, began, ended),
            **document,
        )
        raise typer.Exit(code)
    text = report.render(running.session, elapsed)
    if text:
        print(terminal_text(text, sys.stdout, "\n"))
    raise typer.Exit(code)
