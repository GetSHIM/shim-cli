"""VS Code tool events, as measured against VS Code 1.137.0.

Before a tool runs, `permissionDecision: "deny"` is real: the call is refused
and no `PostToolUse` follows. After one it is not. A probe returned
`decision: "block"` on a terminal result and the model quoted the secret from
it anyway; `continue: false` did not stop that turn either. By the time this
event fires the model has been handed the result, so the only honest action
left is to tell the user.

Two further limits, same measurement: VS Code sends `read_file` results as an
empty `tool_response`, so a file read cannot be inspected here at all — only
its path, before the read — and no event accepts a replacement payload, so
nothing is ever masked.
"""

from __future__ import annotations

import json

from shim_cli.clients.user_prompt_hook import parse_object
from shim_cli.events.pipeline import REFUSE, REPORT_ONLY, Adapter, Event
from shim_cli.policy import ALLOW, DENY, MASK, REPORT

MAX_INPUT_BYTES = 1_000_000
MAX_OUTPUT_BYTES = 1_000_000
_DENY_REASON = "shim: sensitive data detected; this call was not allowed."
_REPEAT_WARNING = (
    "Do not repeat the values this names in your reply, and do not write "
    "them to a file."
)
_ERROR_MESSAGE = "shim: this tool event could not be inspected and was not modified."
# VS Code names tool inputs in camelCase; the snake_case spellings are here
# because a hook file shared with a Claude-shaped client sees both.
_TARGET_KEYS = ("filePath", "file_path", "notebookPath", "notebook_path", "path", "url")
_FILE_VIEW_KEYS = ("filePath", "file_path", "notebookPath", "notebook_path", "path")


def _decoder(expected_event: str, root: str):
    def decode(raw: bytes | dict[str, object]) -> Event:
        if isinstance(raw, bytes) and len(raw) > MAX_INPUT_BYTES:
            raise ValueError("hook input exceeds the safe limit")
        document = parse_object(raw) if isinstance(raw, bytes) else raw
        if document.get("hook_event_name") != expected_event:
            raise ValueError("unexpected tool-hook event")
        tool = document.get("tool_name")
        if tool is None:
            tool = ""
        elif not isinstance(tool, str):
            raise ValueError("tool-hook tool name must be text")

        target = ""
        views_file = False
        tool_input = document.get("tool_input")
        if isinstance(tool_input, dict):
            for key in _TARGET_KEYS:
                value = tool_input.get(key)
                if isinstance(value, str) and value:
                    target = value
                    break
            views_file = any(
                isinstance(tool_input.get(key), str) and tool_input[key]
                for key in _FILE_VIEW_KEYS
            )
        return Event(tool, document.get(root), target, views_file)

    return decode


def _dump(document: dict) -> bytes:
    output = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    if len(output) > MAX_OUTPUT_BYTES:
        raise ValueError("hook output exceeds the safe limit")
    return output


def pre_tool_use(action: str, _payload: object, message: str) -> bytes:
    if action == ALLOW:
        return b""
    if action == REPORT:
        return _dump({"systemMessage": message})
    if action == DENY:
        return _dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": message or _DENY_REASON,
                }
            }
        )
    if action == MASK:
        raise ValueError("VS Code cannot replace a tool input")
    raise ValueError("unsupported action")


def post_tool_use(action: str, _payload: object, message: str) -> bytes:
    if action == ALLOW:
        return b""
    if action == REPORT:
        # additionalContext is the one field that reaches the model here. It
        # cannot unsend the result, but it can stop the model repeating the
        # value into its reply, where it would be read again.
        return _dump(
            {
                "systemMessage": message,
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": f"{message} {_REPEAT_WARNING}",
                },
            }
        )
    if action in (DENY, MASK):
        raise ValueError("VS Code cannot withhold or replace a tool result")
    raise ValueError("unsupported action")


def error_output() -> bytes:
    return _dump({"systemMessage": _ERROR_MESSAGE})


TOOL_EVENTS = {
    "PreToolUse": Adapter(
        "vscode",
        "PreToolUse",
        "tool_input",
        _decoder("PreToolUse", "tool_input"),
        pre_tool_use,
        # Measured: the call is refused and no PostToolUse follows.
        power=REFUSE,
    ),
    "PostToolUse": Adapter(
        "vscode",
        "PostToolUse",
        "tool_response",
        _decoder("PostToolUse", "tool_response"),
        post_tool_use,
        # Measured: neither a block nor continue: false keeps the result from
        # the model, so enforcement here would be a promise shim cannot keep.
        power=REPORT_ONLY,
    ),
}
INSTALLED_EVENTS = tuple(sorted(TOOL_EVENTS))
