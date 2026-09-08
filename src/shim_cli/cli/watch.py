from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time

import typer

from shim_cli.cli.output import emit, emit_json, terminal_text

# Copilot proxying requires BYOK.
BASE_URL_VARIABLES = {
    "claude": "ANTHROPIC_BASE_URL",
    "codex": "OPENAI_BASE_URL",
}
UPSTREAMS = {
    "claude": "api.anthropic.com",
    "codex": "api.openai.com",
}
VERIFIED = frozenset({"claude"})


def watch(*, command: tuple, as_json: bool) -> None:
    if not command:
        _fail(as_json, "Nothing to run. Try: shim watch -- claude")
    client = os.path.basename(command[0])
    variable = BASE_URL_VARIABLES.get(client, "")
    if not variable:
        _fail(
            as_json,
            f"shim watch does not support {client}. Supported: "
            + ", ".join(sorted(BASE_URL_VARIABLES)),
        )
    if os.environ.get(variable):
        _fail(
            as_json,
            f"{variable} is already configured; custom upstreams are unsupported.",
        )
    if shutil.which(command[0]) is None and not os.path.exists(command[0]):
        _fail(as_json, f"{command[0]} was not found on PATH.")

    from shim_cli.guard import evaluate
    from shim_cli.watch import proxy, report

    try:
        running = proxy.start(UPSTREAMS[client], evaluate)
    except OSError as error:
        _fail(as_json, f"The proxy could not start ({error}); nothing was run.")

    if not as_json:
        emit("PASS", f"Watching {client} on {running.base_url}. Nothing is modified.")
        if client not in VERIFIED:
            emit(
                "WARN",
                f"{client} behind a proxy is unverified; sign-in may not work.",
            )

    environment = dict(os.environ)
    environment[variable] = running.base_url
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, env=environment)
        try:
            code = process.wait()
        except KeyboardInterrupt:
            process.send_signal(signal.SIGINT)
            code = process.wait()
    except OSError as error:
        _fail(as_json, f"{command[0]} could not be started ({error}).")
    finally:
        running.stop()

    elapsed = time.monotonic() - started
    if as_json:
        emit_json(
            "watch", "ok", exit_code=code, **report.as_json(running.session, elapsed)
        )
        raise typer.Exit(code)
    text = report.render(running.session, elapsed)
    if text:
        print(terminal_text(text, sys.stdout, "\n"))
    raise typer.Exit(code)


def _fail(as_json: bool, message: str):
    if as_json:
        emit_json("watch", "error", error=message)
    else:
        emit("FAIL", message, error=True)
    raise typer.Exit(2)
