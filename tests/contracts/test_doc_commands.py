"""Every `shim ...` line a visitor can copy names a real command and real flags.

The CLI's own command tree is the reference, so a renamed flag fails here
before a reader finds it. Commands are not run: only their names, options and
literal choice arguments are checked.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import click
import pytest
from typer.main import get_command

from shim_cli.cli.app import app

ROOT = Path(__file__).resolve().parents[2]
DOCS = (
    "README.md",
    "plugins/shim-cli/README.md",
    "docs/cookbook.md",
    "docs/commands.md",
    "docs/findings.md",
    "llms.txt",
)
SHELL_FENCES = {"console", "bash", "sh", "shell", "zsh", "yaml", "yml"}
FENCE = re.compile(r"^\s*```+\s*([\w-]*)")
INLINE = re.compile(r"`(shim(?: [^`]*)?)`")
# Where one shell command ends and the next may start.
SEPARATOR = re.compile(r"\|\||&&|[|;(]|\$\(")
LEADING = {"$", "if", "!", "then", "do", "run:", "-", "time", "exec"}
PLACEHOLDER = re.compile(r"^(<.*>|\.\.\.|…|[A-Z][A-Z_]*|\[.*\])$")
# Named in the docs as what does not exist.
NOT_COMMANDS = {"shim install vscode"}


def _invocations(text: str) -> list[tuple[int, str]]:
    """(line number, command line) for every shim call a reader could copy."""
    found = []
    lines = text.splitlines()
    fence = None
    block: list[tuple[int, str]] = []
    for number, line in enumerate(lines, 1):
        opened = FENCE.match(line)
        if opened and fence is None:
            fence = opened.group(1).lower()
            block = []
            continue
        if opened:
            if fence in SHELL_FENCES:
                prompted = [(n, b) for n, b in block if b.lstrip().startswith("$ ")]
                for n, b in prompted or block:
                    found.extend((n, part) for part in _segments(b))
            fence = None
            continue
        if fence is not None:
            block.append((number, line))
            continue
        for span in INLINE.findall(line):
            if span.endswith(".") or ": " in span or span in NOT_COMMANDS:
                continue  # a line shim prints, or a command that is not there
            found.extend((number, part) for part in _segments(span))
    return found


def _segments(line: str) -> list[str]:
    line = line.split(" #", 1)[0]
    calls = []
    for part in SEPARATOR.split(line):
        words = part.strip().split()
        while words and (words[0] in LEADING or re.match(r"^\w+=\S*$", words[0])):
            words.pop(0)
        if words and words[0] == "shim":
            calls.append(" ".join(words))
    return calls


def _tokens(call: str) -> list[str]:
    try:
        tokens = shlex.split(call)
    except ValueError:
        tokens = call.split()
    kept = []
    for token in tokens:
        if token in {"<", ">", ">>", "2>", "2>&1", ")"} or token.startswith(">"):
            break
        kept.append(token.rstrip(")"))
    return kept


def _options(command: click.Command) -> dict[str, click.Option]:
    known = {"--help": None}
    for param in command.params:
        if isinstance(param, click.Option):
            for name in (*param.opts, *param.secondary_opts):
                known[name] = param
    return known


def _problem(call: str, root: click.Group) -> str | None:
    tokens = _tokens(call)[1:]
    command: click.Command = root
    words: list[str] = []
    while isinstance(command, click.Group):
        options = _options(command)
        while tokens and tokens[0].startswith("-") and tokens[0] != "--":
            flag = tokens.pop(0).split("=", 1)[0]
            if flag not in options:
                return f"`shim {' '.join(words)}` has no option {flag}"
        if not tokens or PLACEHOLDER.match(tokens[0]):
            return None
        name = tokens.pop(0)
        if not re.match(r"^[a-z][a-z-]*$", name):
            return None  # an output line such as "shim — this session"
        if name not in command.commands:
            return f"`shim {' '.join(words)}` has no command {name!r}"
        words.append(name)
        command = command.commands[name]
    options = _options(command)
    arguments = [p for p in command.params if isinstance(p, click.Argument)]
    while tokens:
        token = tokens.pop(0)
        if token == "--":
            break
        if token.startswith("-") and token != "-":
            flag, _, inline = token.partition("=")
            if flag not in options:
                return f"`shim {' '.join(words)}` has no option {flag}"
            option = options[flag]
            if option is not None and not option.is_flag and not inline and tokens:
                tokens.pop(0)  # its value
            continue
        if PLACEHOLDER.match(token) or not arguments:
            continue
        choices = getattr(arguments[0].type, "choices", None)
        if choices and token not in choices:
            return f"`shim {' '.join(words)}` does not accept {token!r}"
        if arguments[0].nargs == 1:
            arguments.pop(0)
    return None


@pytest.mark.parametrize("path", DOCS)
def test_every_documented_shim_command_exists(path: str) -> None:
    root = get_command(app)
    assert isinstance(root, click.Group)
    calls = _invocations((ROOT / path).read_text(encoding="utf-8"))

    problems = [
        f"{path}:{number}: {call} — {problem}"
        for number, call in calls
        if (problem := _problem(call, root))
    ]

    assert calls, f"{path} shows no shim command; the parser lost them"
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    ("call", "expected"),
    (
        ("shim install vscode", "does not accept 'vscode'"),
        ("shim doctor claude --verbose", "has no option --verbose"),
        ("shim ledger list", "has no command 'list'"),
        ("shim stats", "has no command 'stats'"),
        ("shim audit --since 2026-09-01 --project ~/work/x --json", None),
        ("shim config --reveal IBAN=4 --yes", None),
        ("shim watch --json -- claude -p hi", None),
        ("shim doctor <client>", None),
        ("shim — this session", None),
    ),
)
def test_the_checker_catches_what_a_reader_would_trip_on(
    call: str, expected: str | None
) -> None:
    root = get_command(app)
    assert isinstance(root, click.Group)

    problem = _problem(call, root)

    assert problem == expected or (expected and problem and expected in problem)


def test_shell_lines_are_split_where_a_reader_would_split_them() -> None:
    text = (
        "```console\n"
        "$ kubectl logs api | shim redact | pbcopy\n"
        "$ if shim scan --json < diff.txt; then echo ok; fi\n"
        "output line shim nothing\n"
        "```\n"
        "Run `shim doctor codex` and `shim-hook`; it says `shim blocked this: X.`\n"
    )

    assert [call for _, call in _invocations(text)] == [
        "shim redact",
        "shim scan --json < diff.txt",
        "shim doctor codex",
    ]
