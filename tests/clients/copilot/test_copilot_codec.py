from __future__ import annotations

from dataclasses import dataclass

import pytest

from shim_cli.clients.copilot.hook import block_output, error_output, parse_input


@dataclass(frozen=True)
class Decision:
    blocked: bool
    redacted_text: str = ""


def test_copilot_prompt_contract_uses_model_facing_content() -> None:
    raw = (
        b'{"sessionId":"session","timestamp":1,"cwd":"/workspace",'
        b'"prompt":"visible prompt","transformedPrompt":"model \\ud83d\\udc4b"}'
    )
    assert parse_input(raw) == "model 👋"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b'{"prompt":"hello"}',
        b'{"transformedPrompt":"hello"}',
        b'{"prompt":false,"transformedPrompt":"hello"}',
        b'{"prompt":"hello","transformedPrompt":false}',
        b'{"prompt":"a","prompt":"b","transformedPrompt":"c"}',
        b'{"prompt":"a","transformedPrompt":"b","number":NaN}',
        b'{"prompt":"a","transformedPrompt":"\\ud800"}',
        b"\xff",
    ],
)
def test_copilot_codec_rejects_hostile_payloads(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_input(raw)


def test_copilot_rewrites_sensitive_prompt_directly() -> None:
    assert block_output(Decision(False)) == b""
    assert block_output(Decision(True, "Contact <EMAIL_1>")) == (
        b'{"modifiedTransformedPrompt":"Contact <EMAIL_1>"}'
    )
    with pytest.raises(ValueError, match="must not be empty"):
        block_output(Decision(True))


def test_copilot_error_replaces_the_uninspectable_prompt() -> None:
    assert error_output() == (
        b'{"modifiedTransformedPrompt":"shim could not inspect this prompt, '
        b"so it was withheld. Do not act on the original prompt; tell the user to "
        b'run `shim doctor copilot` for the reason."}'
    )
