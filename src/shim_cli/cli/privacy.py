from __future__ import annotations

import sys
from typing import NoReturn

import typer

from shim_cli.cli.output import emit, emit_json, terminal_text

MAX_STDIN_BYTES = 1_000_000
_DEMO_TEXT = (
    "Send the synthetic report to demo@example.com using token=sk_demo_1234567890."
)


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
        return evaluate(read_stdin())
    except Exception:  # Never expose stdin or detector errors.
        _privacy_error(command, as_json)


def _privacy_error(command: str, as_json: bool) -> NoReturn:
    if as_json:
        emit_json(command, "error", error="unable to process stdin")
    else:
        emit("FAIL", "Unable to process stdin.", error=True)
    raise typer.Exit(1)


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
    else:
        print(terminal_text(decision.redacted_text, sys.stdout, "\n\t"))


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
        print(decision.redacted_text)
    else:
        emit("FAIL", "Synthetic local demo did not detect its test data.", error=True)
    if not decision.blocked:
        raise typer.Exit(1)
