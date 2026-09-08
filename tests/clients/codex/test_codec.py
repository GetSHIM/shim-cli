from __future__ import annotations

from dataclasses import dataclass

import pytest

from shim_cli.clients.codex.hook import (
    MAX_OUTPUT_BYTES,
    MAX_REASON_CHARS,
    block_output,
    error_output,
    parse_input,
)


@dataclass(frozen=True)
class Decision:
    blocked: bool
    counts: tuple[tuple[str, int], ...] = ()
    redacted_text: str = ""


def test_parse_exact_codex_prompt_contract() -> None:
    raw = (
        b'{"session_id":"thread","hook_event_name":"UserPromptSubmit",'
        b'"turn_id":"turn","prompt":"hello \\ud83d\\udc4b"}'
    )
    assert parse_input(raw) == "hello 👋"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b'{"hook_event_name":"Stop","prompt":"hello"}',
        b'{"hook_event_name":"UserPromptSubmit"}',
        b'{"hook_event_name":"UserPromptSubmit","prompt":false}',
        b'{"hook_event_name":"UserPromptSubmit","prompt":"a","prompt":"b"}',
        b'{"hook_event_name":"UserPromptSubmit","prompt":"a","number":NaN}',
        b'{"hook_event_name":"UserPromptSubmit","prompt":"a","number":1e999}',
        b'{"hook_event_name":"UserPromptSubmit","prompt":"\\ud800"}',
        b"\xff",
    ],
)
def test_parse_rejects_hostile_payloads(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_input(raw)


def test_safe_decision_emits_nothing() -> None:
    assert block_output(Decision(False)) == b""


def test_block_is_exact_compact_native_json() -> None:
    output = block_output(
        Decision(True, (("EMAIL", 1), ("SECRET", 2))),
        "/tmp/shim-redacted-test.txt",
    )
    assert output == (
        b'{"decision":"block","reason":"shim blocked this prompt: '
        b"EMAIL (1), SECRET (2).\\nCopy and paste this as your next prompt:\\n"
        b"Read this file and use its contents as my prompt: "
        b'/tmp/shim-redacted-test.txt"}'
    )


@pytest.mark.parametrize(
    "path",
    [None, "relative.txt", "/tmp/../unsafe.txt", "/tmp/unsafe\x1b.txt"],
)
def test_block_rejects_unsafe_suggestion_paths(path: str | None) -> None:
    with pytest.raises(ValueError, match="path"):
        block_output(Decision(True, (("SECRET", 1),)), path)


def test_block_output_remains_bounded() -> None:
    with pytest.raises(ValueError, match="4,000"):
        block_output(Decision(True, (("SECRET", 1),)), "/" + "x" * MAX_REASON_CHARS)

    output = block_output(Decision(True, (("SECRET", 1),)), "/tmp/prompt.txt")
    assert len(output) <= MAX_OUTPUT_BYTES


def test_error_block_is_generic_and_compact() -> None:
    assert error_output() == (
        b'{"decision":"block","reason":"shim could not inspect this '
        b'prompt, so it was withheld. Run `shim doctor codex` for the reason."}'
    )
