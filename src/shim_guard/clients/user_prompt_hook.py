from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shim_guard.guard import GuardDecision

EVENT_NAME = "UserPromptSubmit"
MAX_REASON_CHARS = 4_000
MAX_OUTPUT_BYTES = 4_096
_ERROR_REASON = (
    "SHIM Guard could not inspect this prompt, so it was withheld. "
    "Run `shim doctor {client}` for the reason."
)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate JSON key")
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def parse_object(raw: bytes) -> dict[str, object]:
    try:
        payload = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
            parse_float=_float,
        )
    except (UnicodeDecodeError, RecursionError, ValueError) as error:
        raise ValueError("invalid prompt-hook payload") from error
    if not isinstance(payload, dict):
        raise ValueError("prompt-hook payload must be an object")
    return payload


def parse_input(raw: bytes | dict[str, object]) -> str:
    payload = parse_object(raw) if isinstance(raw, bytes) else raw
    if payload.get("hook_event_name") != EVENT_NAME:
        raise ValueError("unexpected prompt-hook event")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        raise ValueError("prompt-hook prompt must be a string")
    try:
        prompt.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError("prompt-hook prompt must contain valid Unicode") from error
    return prompt


def _json_block(reason: str, suppress_original_prompt: bool) -> bytes:
    if not reason or len(reason) > MAX_REASON_CHARS:
        raise ValueError("block reason must contain at most 4,000 characters")
    document: dict[str, object] = {"decision": "block", "reason": reason}
    if suppress_original_prompt:
        document["suppressOriginalPrompt"] = True
    output = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    if len(output) > MAX_OUTPUT_BYTES:
        raise ValueError("block output exceeds 4,096 bytes")
    return output


def block_output(
    decision: GuardDecision,
    suggestion_path: str | None,
    *,
    suppress_original_prompt: bool = False,
) -> bytes:
    if not decision.blocked:
        return b""
    if not isinstance(suggestion_path, str) or not suggestion_path:
        raise ValueError("suggestion path is invalid")
    path = Path(suggestion_path)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or not suggestion_path.isprintable()
    ):
        raise ValueError("suggestion path is invalid")
    counts = ", ".join(f"{category} ({count})" for category, count in decision.counts)
    reason = (
        f"SHIM Guard blocked this prompt: {counts}.\n"
        "Copy and paste this as your next prompt:\n"
        f"Read this file and use its contents as my prompt: {suggestion_path}"
    )
    return _json_block(reason, suppress_original_prompt)


def warn_output(decision: GuardDecision) -> bytes:
    if not decision.blocked:
        return b""
    counts = ", ".join(f"{category} ({count})" for category, count in decision.counts)
    document = {"systemMessage": f"shim: found {counts} in your prompt. Not modified."}
    output = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    if len(output) > MAX_OUTPUT_BYTES:
        raise ValueError("warn output exceeds 4,096 bytes")
    return output


def error_output(client: str, *, suppress_original_prompt: bool = False) -> bytes:
    return _json_block(_ERROR_REASON.format(client=client), suppress_original_prompt)
