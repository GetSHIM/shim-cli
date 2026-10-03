from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shim_cli.guard import GuardDecision

EVENT_NAME = "UserPromptSubmit"
MAX_REASON_CHARS = 4_000
MAX_OUTPUT_BYTES = 4_096
_ERROR_REASON = (
    "shim could not inspect this prompt, so it was withheld. "
    "Run `shim doctor {client}` for the reason."
)
_ATTACHED = (
    "Attached files reach the model unmasked; to have them masked, ask the "
    "agent to read the file instead."
)
_SHOWN_FILES = 3


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


def _listed(counts) -> str:
    return ", ".join(f"{category} ({count})" for category, count in counts)


def held_sentence(held: list[tuple[str, tuple]]) -> str:
    shown = "; ".join(
        f"@{name} holds {_listed(counts)}" for name, counts in held[:_SHOWN_FILES]
    )
    more = len(held) - _SHOWN_FILES
    return f"{shown}{f', and {more} more' if more > 0 else ''}. {_ATTACHED}"


def unread_sentence(names: list[str]) -> str:
    shown = ", ".join(f"@{name}" for name in names[:_SHOWN_FILES])
    if len(names) > _SHOWN_FILES:
        shown += f" and {len(names) - _SHOWN_FILES} more"
    if len(names) == 1:
        return f"shim: {shown} was not inspected. It reaches the model as it is."
    return f"shim: {shown} were not inspected. They reach the model as they are."


def block_reason(
    decision: GuardDecision, suggestion_path: str | None, held: tuple = ()
) -> str:
    if not decision.blocked:
        return f"shim blocked this prompt: {held_sentence(list(held))}"
    if not isinstance(suggestion_path, str) or not suggestion_path:
        raise ValueError("suggestion path is invalid")
    path = Path(suggestion_path)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or not suggestion_path.isprintable()
    ):
        raise ValueError("suggestion path is invalid")
    reason = (
        f"shim blocked this prompt: {_listed(decision.counts)}.\n"
        "Copy and paste this as your next prompt:\n"
        f"Read this file and use its contents as my prompt: {suggestion_path}"
    )
    return f"{reason}\n{held_sentence(list(held))}" if held else reason


def block_output(
    decision: GuardDecision,
    suggestion_path: str | None,
    *,
    suppress_original_prompt: bool = False,
    held: tuple = (),
) -> bytes:
    if not decision.blocked and not held:
        return b""
    return _json_block(
        block_reason(decision, suggestion_path, held), suppress_original_prompt
    )


def warn_output(decision: GuardDecision, held: tuple = (), unread: tuple = ()) -> bytes:
    parts = []
    if decision.blocked:
        parts.append(
            f"shim: found {_listed(decision.counts)} in your prompt. Not modified."
        )
    if held:
        parts.append(f"shim: {held_sentence(list(held))}")
    if unread:
        parts.append(unread_sentence(list(unread)))
    if not parts:
        return b""
    document = {"systemMessage": " ".join(parts)}
    output = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    if len(output) > MAX_OUTPUT_BYTES:
        raise ValueError("warn output exceeds 4,096 bytes")
    return output


def error_output(client: str, *, suppress_original_prompt: bool = False) -> bytes:
    return _json_block(_ERROR_REASON.format(client=client), suppress_original_prompt)
