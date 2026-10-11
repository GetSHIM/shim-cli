"""Every error code shim-cli emits is documented, and every documented code is
emitted. An agent acts on `code` and `fix`; an undocumented code is one it
cannot look up, and a documented one nothing emits is a promise nobody keeps.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import click
from typer.main import get_command

from shim_cli.cli.app import app

ROOT = Path(__file__).resolve().parents[2]
CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,47}$")


def _code_argument(call: ast.Call) -> ast.expr | None:
    name = getattr(call.func, "id", getattr(call.func, "attr", ""))
    position = {"emit_error": 1, "Check": 3}.get(name)
    if position is None:
        return None
    for keyword in call.keywords:
        if keyword.arg == "code":
            return keyword.value
    if len(call.args) > position:
        return call.args[position]
    if name == "emit_error":
        raise AssertionError("emit_error called without a code")
    return None


def _emitted() -> set[str]:
    found = set()
    for path in sorted((ROOT / "src" / "shim_cli" / "cli").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            argument = _code_argument(node)
            if argument is None:
                continue
            where = f"{path.name}:{node.lineno}"
            assert isinstance(argument, ast.Constant), f"{where}: code is not a literal"
            assert isinstance(argument.value, str), f"{where}: code is not a string"
            assert CODE.fullmatch(argument.value), f"{where}: {argument.value!r}"
            found.add(argument.value)
    return found


def _documented() -> set[str]:
    text = (ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
    section = text[text.index("\n## Error codes\n") :]
    section = section[: section.index("\n## ", 1)]
    return set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", section, re.M))


def test_every_emitted_code_is_documented_and_every_documented_code_emitted():
    emitted, documented = _emitted(), _documented()

    assert emitted - documented == set(), "emitted but not in docs/commands.md"
    assert documented - emitted == set(), "documented but nothing emits it"


def _commands(group: click.Group, prefix: str = ""):
    for name, command in group.commands.items():
        if isinstance(command, click.Group):
            yield from _commands(command, f"{prefix}{name} ")
        else:
            yield f"{prefix}{name}", command


def test_every_command_but_help_and_update_takes_json():
    missing = [
        name
        for name, command in _commands(get_command(app))
        if name not in {"help", "update"}
        and not any("--json" in parameter.opts for parameter in command.params)
    ]

    assert missing == []
