"""VS Code agent hooks.

A prompt hook here can report or refuse, and nothing else: the accepted output
is `continue`, `stopReason` and `systemMessage`, with no field that replaces the
text the model reads. So the redacted prompt is written to a file and named in
the reason, as it is for Codex, rather than substituted the way Copilot CLI
substitutes it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from shim_cli.clients import user_prompt_hook

if TYPE_CHECKING:
    from shim_cli.guard import GuardDecision

MAX_OUTPUT_BYTES = user_prompt_hook.MAX_OUTPUT_BYTES
_ERROR_REASON = (
    "shim could not inspect this prompt, so it was withheld. "
    "Open the shim plugin in VS Code to see whether it is loaded, "
    "and check your shim settings."
)

parse_input = user_prompt_hook.parse_input
warn_output = user_prompt_hook.warn_output


def _stop(reason: str) -> bytes:
    if not reason or len(reason) > user_prompt_hook.MAX_REASON_CHARS:
        raise ValueError("stop reason must contain at most 4,000 characters")
    output = json.dumps(
        {"continue": False, "stopReason": reason},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    if len(output) > MAX_OUTPUT_BYTES:
        raise ValueError("stop output exceeds 4,096 bytes")
    return output


def block_output(decision: GuardDecision, suggestion_path: str | None = None) -> bytes:
    if not decision.blocked:
        return b""
    return _stop(user_prompt_hook.block_reason(decision, suggestion_path))


def error_output() -> bytes:
    return _stop(_ERROR_REASON)
