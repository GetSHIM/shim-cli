from __future__ import annotations

import sys
from typing import NoReturn

import typer

from shim_cli.cli.output import emit, emit_error, emit_json, terminal_text

MAX_STDIN_BYTES = 1_000_000
_DEMO_TEXT = (
    "Send the synthetic report to demo@example.com using token=sk_demo_1234567890."
)
# What each client's hook does with that sentence by default (README, Supported clients).
_DEMO_OUTCOME = {
    "claude": "In Claude Code, a prompt you type like this is reported and sent "
    "unchanged (stopped under enforce); a tool result like it reaches the model "
    "masked.",
    "codex": "In Codex, a prompt you type like this is reported and sent unchanged "
    "(stopped under enforce); tool calls and results are not inspected.",
    "copilot": "In GitHub Copilot CLI, a prompt you type like this reaches the "
    "model masked; tool calls and results are not inspected.",
}


def read_stdin() -> str:
    source = getattr(sys.stdin, "buffer", sys.stdin)
    data = source.read(MAX_STDIN_BYTES + 1)
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > MAX_STDIN_BYTES:
        raise ValueError("input is too large")
    return data.decode("utf-8")


def evaluate(text: str):
    from shim_cli.config import load_policy
    from shim_cli.guard import evaluate as evaluate_guard

    policy = load_policy()
    return evaluate_guard(text, policy.entities, policy.custom, policy.reveal)


def _read_and_evaluate(command: str, as_json: bool):
    try:
        decision = evaluate(read_stdin())
    except Exception:  # Never expose stdin or detector errors.
        _privacy_error(command, as_json)
    if decision.partial:
        _privacy_error(command, as_json)
    return decision


def _privacy_error(command: str, as_json: bool) -> NoReturn:
    emit_error(
        command,
        "STDIN_UNPROCESSABLE",
        "Unable to process stdin.",
        "Pipe UTF-8 text; if the settings file is refused, shim config prints why.",
        as_json=as_json,
        exit_code=1,
    )


def scan(*, as_json: bool) -> None:
    decision = _read_and_evaluate("scan", as_json)
    counts = dict(decision.counts)
    if as_json:
        emit_json("scan", "findings" if decision.blocked else "safe", counts=counts)
    elif decision.blocked:
        categories = ", ".join(f"{name}: {count}" for name, count in counts.items())
        emit("WARN", f"Sensitive data found ({categories}).")
    else:
        emit("PASS", "No supported sensitive data found.")
    if decision.blocked:
        raise typer.Exit(1)


def redact(*, as_json: bool) -> None:
    decision = _read_and_evaluate("redact", as_json)
    if as_json:
        emit_json(
            "redact",
            "findings" if decision.blocked else "safe",
            counts=dict(decision.counts),
        )
    elif sys.stdout.isatty():
        # Escape what the terminal's codec cannot show instead of crashing on it.
        shown = terminal_text(decision.redacted_text, sys.stdout, "\n\t")
        encoding = sys.stdout.encoding
        sys.stdout.write(shown.encode(encoding, "backslashreplace").decode(encoding))
    else:
        # UTF-8 like read_stdin, not the locale's codec, and the input's own line
        # ending, so `shim redact < f > g` changes only values.
        sys.stdout.buffer.write(decision.redacted_text.encode("utf-8"))


def demo(*, client: str, as_json: bool) -> None:
    try:
        from shim_cli.guard import evaluate as evaluate_guard

        decision = evaluate_guard(_DEMO_TEXT)
    except Exception:
        _privacy_error("demo", as_json)
    if as_json:
        emit_json(
            "demo",
            "findings" if decision.blocked else "error",
            client=client,
            counts=dict(decision.counts),
        )
    elif decision.blocked:
        emit("PASS", "Synthetic local demo detected sensitive data.")
        print(f"  before  {_DEMO_TEXT}")
        print(f"  after   {decision.redacted_text}")
        print(_DEMO_OUTCOME[client])
    else:
        emit("FAIL", "Synthetic local demo did not detect its test data.", error=True)
    if not decision.blocked:
        raise typer.Exit(1)
