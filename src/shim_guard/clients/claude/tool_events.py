from __future__ import annotations

import json

from shim_guard.clients.user_prompt_hook import parse_object
from shim_guard.events.pipeline import Adapter, Event
from shim_guard.policy import ALLOW, DENY, MASK, REPORT

MAX_INPUT_BYTES = 1_000_000
MAX_OUTPUT_BYTES = 1_000_000
_DENY_REASON = "SHIM Guard: sensitive data detected; this call was not allowed."
_ERROR_MESSAGE = "shim: this tool event could not be inspected and was not modified."
_TARGET_KEYS = ("file_path", "notebook_path", "path", "url")
_FILE_VIEW_KEYS = ("file_path", "notebook_path", "path")


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


def _specific(event: str, **fields) -> dict:
    return {"hookSpecificOutput": dict({"hookEventName": event}, **fields)}


def pre_tool_use(action: str, payload: object, message: str) -> bytes:
    if action == ALLOW:
        return b""
    if action == REPORT:
        return _dump({"systemMessage": message})
    if action == MASK:
        output = _specific(
            "PreToolUse", permissionDecision="allow", updatedInput=payload
        )
        if message:
            output["systemMessage"] = message
        return _dump(output)
    if action == DENY:
        return _dump(
            _specific(
                "PreToolUse",
                permissionDecision="deny",
                permissionDecisionReason=message or _DENY_REASON,
            )
        )
    raise ValueError("unsupported action")


def post_tool_use(action: str, payload: object, message: str) -> bytes:
    if action == ALLOW:
        return b""
    if action == REPORT:
        return _dump({"systemMessage": message})
    if action == MASK:
        output = _specific("PostToolUse", updatedToolOutput=payload)
        if message:
            output["systemMessage"] = message
        return _dump(output)
    if action == DENY:
        raise ValueError("a tool result cannot be denied")
    raise ValueError("unsupported action")


def error_output() -> bytes:
    return _dump({"systemMessage": _ERROR_MESSAGE})


TOOL_EVENTS = {
    "PreToolUse": Adapter(
        "claude",
        "PreToolUse",
        "tool_input",
        _decoder("PreToolUse", "tool_input"),
        pre_tool_use,
    ),
    "PostToolUse": Adapter(
        "claude",
        "PostToolUse",
        "tool_response",
        _decoder("PostToolUse", "tool_response"),
        post_tool_use,
    ),
}
INSTALLED_EVENTS = tuple(sorted(TOOL_EVENTS))


def coverage() -> tuple:
    return tuple(
        {
            "event": event,
            "sees": TOOL_EVENTS[event].root,
            "can_mask": True,
            "can_report": True,
            "verified": True,
            "installed": True,
        }
        for event in INSTALLED_EVENTS
    )
