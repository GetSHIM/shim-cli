from __future__ import annotations

import errno
import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TextIO

import typer
from rich.console import Console
from rich.text import Text

SCHEMA_VERSION = 1
FIX_CONFIRMATION = "Add --yes to apply without a question."
# A write refused like this is refused again: "run the command again" is untrue.
_UNWRITABLE = {
    errno.EACCES: "Give yourself write access to {folder} (chmod u+rwx {folder}), "
    "then run the command again.",
    errno.EPERM: "The system refused the change: check the owner, group and flags "
    "of {target} and {folder}, then run the command again.",
    errno.EROFS: "{folder} is on a read-only file system; make it writable, then "
    "run the command again.",
    errno.ENOSPC: "Free space on the disk that holds {folder}, then run the command "
    "again.",
}


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: str
    detail: str
    code: str | None = None
    fix: str | None = None


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


def unwritable(error: BaseException | None, target: Path) -> tuple[str, str] | None:
    """Why writing `target` failed for good, and the fix; None for a race."""
    while error is not None:
        if isinstance(error, OSError) and error.errno in _UNWRITABLE:
            folder = next(
                (path for path in target.parents if path.is_dir()), target.parent
            )
            fix = _UNWRITABLE[error.errno].format(
                folder=shlex.quote(str(folder)), target=shlex.quote(str(target))
            )
            return error.strerror or os.strerror(error.errno), fix
        error = error.__cause__
    return None


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
