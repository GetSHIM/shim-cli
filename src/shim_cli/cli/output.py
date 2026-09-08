from __future__ import annotations

import json
import os
import sys
from typing import Any, TextIO

from rich.console import Console
from rich.text import Text

SCHEMA_VERSION = 1


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
