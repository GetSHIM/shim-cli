from __future__ import annotations

import json
import os
import sys
from typing import Any, NoReturn, TextIO

import typer
from rich.console import Console
from rich.text import Text

SCHEMA_VERSION = 1
FIX_CONFIRMATION = "Add --yes to apply without a question."


def terminal_text(text: str, stream: TextIO, allowed: str = "") -> str:
    if not stream.isatty():
        return text
    return "".join(
        character
        if character in allowed or character.isprintable()
        else character.encode("unicode_escape").decode("ascii")
        for character in text
    )


def emit_json(command: str, status: str, **data: Any) -> None:
    payload = {"schema_version": SCHEMA_VERSION, "command": command, "status": status}
    payload.update(data)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def emit_error(
    command: str,
    code: str,
    message: str,
    fix: str | None,
    *,
    as_json: bool,
    exit_code: int = 2,
    plain: bool = False,
    then: str | None = None,
    **data: Any,
) -> NoReturn:
    """`code` is a literal at the call site; docs/commands.md lists every one.

    The human form is what each command printed before codes existed:
    `FAIL <message>`, `shim: <message>.` with `plain`, and a `WARN` line after
    it with `then`.
    """
    if as_json:
        emit_json(command, "error", error=message, code=code, fix=fix, **data)
    elif plain:
        typer.echo(f"shim: {message}.", err=True)
    else:
        emit("FAIL", message, error=True)
        if then is not None:
            emit("WARN", then, error=True)
    raise typer.Exit(exit_code)


def console(stream: TextIO | None = None) -> Console:
    target = sys.stdout if stream is None else stream
    color = target.isatty() and "NO_COLOR" not in os.environ
    return Console(
        file=target,
        force_terminal=color,
        color_system="standard" if color else None,
    )


def emit(label: str, message: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    style = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}.get(label, "")
    line = Text(label, style=style)
    line.append(f" {terminal_text(message, stream)}")
    console(stream).print(line, highlight=False, markup=False)
