from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time

import typer

from shim_cli.cli.output import emit, emit_error, emit_json, terminal_text

# Copilot proxying requires BYOK.
BASE_URL_VARIABLES = {"claude": "ANTHROPIC_BASE_URL"}
UPSTREAMS = {"claude": "api.anthropic.com"}

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
            f"{variable} is already configured; custom upstreams are unsupported. "
            f"Run shim watch with {variable} unset, or use the hook "
            f"(shim install {client}), which does not need the proxy.",
            f"Unset {variable} for this command.",
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

    from shim_cli.guard import evaluate
    from shim_cli.watch import proxy, report

    try:
        running = proxy.start(UPSTREAMS[client], evaluate)
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
    try:
        process = subprocess.Popen(command, env=environment)
        try:
            code = process.wait()
        except KeyboardInterrupt:
            process.send_signal(signal.SIGINT)
            code = process.wait()
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
    if as_json:
        emit_json(
            "watch", "ok", exit_code=code, **report.as_json(running.session, elapsed)
        )
        raise typer.Exit(code)
    text = report.render(running.session, elapsed)
    if text:
        print(terminal_text(text, sys.stdout, "\n"))
    raise typer.Exit(code)
