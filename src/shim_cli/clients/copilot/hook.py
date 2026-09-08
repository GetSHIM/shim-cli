from __future__ import annotations

import json
from typing import TYPE_CHECKING

from shim_cli.clients import user_prompt_hook

if TYPE_CHECKING:
    from shim_cli.guard import GuardDecision

_ERROR_PROMPT = (
    "shim could not inspect this prompt, so it was withheld. Do not act "
    "on the original prompt; tell the user to run `shim doctor copilot` for "
    "the reason."
)


def parse_input(raw: bytes | dict[str, object]) -> str:
    payload = user_prompt_hook.parse_object(raw) if isinstance(raw, bytes) else raw
    prompt = payload.get("prompt")
    transformed = payload.get("transformedPrompt")
    if not isinstance(prompt, str) or not isinstance(transformed, str):
        raise ValueError("Copilot prompt-hook fields must be strings")
    try:
        prompt.encode("utf-8", errors="strict")
        transformed.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError(
            "Copilot prompt-hook fields must contain valid Unicode"
        ) from error
    return transformed


def _rewrite(text: str) -> bytes:
    if not text:
        raise ValueError("Copilot prompt rewrite must not be empty")
    return json.dumps(
        {"modifiedTransformedPrompt": text},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def warn_output(decision: GuardDecision) -> bytes:
    return block_output(decision)


def block_output(decision: GuardDecision, _suggestion_path: str | None = None) -> bytes:
    return _rewrite(decision.redacted_text) if decision.blocked else b""


def error_output() -> bytes:
    return _rewrite(_ERROR_PROMPT)
