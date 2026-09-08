from __future__ import annotations

from dataclasses import dataclass

USER_PROMPT = "user-prompt"
OUTBOUND = "outbound"
INBOUND = "inbound"
LOCAL_WRITE = "local-write"
EXECUTABLE_TEXT = "executable-text"
DIRECTIONS = (USER_PROMPT, OUTBOUND, INBOUND, LOCAL_WRITE, EXECUTABLE_TEXT)

OBSERVE = "observe"
WARN = "warn"
ENFORCE = "enforce"
MODES = (OBSERVE, WARN, ENFORCE)

ALLOW = "allow"
REPORT = "report"
MASK = "mask"
DENY = "deny"

DEFAULT_MODES = {
    USER_PROMPT: WARN,
    OUTBOUND: ENFORCE,
    INBOUND: ENFORCE,
    LOCAL_WRITE: WARN,
    EXECUTABLE_TEXT: WARN,
}

REWRITABLE = {
    USER_PROMPT: False,
    OUTBOUND: True,
    INBOUND: True,
    LOCAL_WRITE: False,
    EXECUTABLE_TEXT: False,
}

LOCAL_WRITE_TOOLS = frozenset(
    {"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "ApplyPatch"}
)
COMMAND_TOOLS = frozenset({"Bash", "BashOutput", "Shell", "PowerShell", "shell"})

_PROMPT_EVENTS = frozenset({"UserPromptSubmit", "userPromptTransformed"})
_INPUT_EVENTS = frozenset({"PreToolUse", "preToolUse"})
_RESULT_EVENTS = frozenset(
    {"PostToolUse", "postToolUse", "PostToolUseFailure", "PostToolBatch"}
)


@dataclass(frozen=True)
class Policy:
    entities: tuple
    modes: dict
    tool_entities: dict
    ledger: bool = False
    diet: tuple = ()

    def mode_for(self, direction: str, tool: str = "", event: str = "") -> str:
        for key in (tool, event, direction):
            if key and key in self.modes:
                return self.modes[key]
        if "default" in self.modes:
            return self.modes["default"]
        return DEFAULT_MODES.get(direction, WARN)

    def entities_for(self, tool: str = "", event: str = "") -> tuple:
        for key in (tool, event):
            if key and key in self.tool_entities:
                return self.tool_entities[key]
        return self.entities


def direction_for(event: str, tool: str) -> str:
    if event in _PROMPT_EVENTS:
        return USER_PROMPT
    if event in _RESULT_EVENTS:
        return INBOUND
    if event in _INPUT_EVENTS:
        if tool in COMMAND_TOOLS:
            return EXECUTABLE_TEXT
        if tool in LOCAL_WRITE_TOOLS:
            return LOCAL_WRITE
        return OUTBOUND
    raise ValueError("unsupported hook event")


def decide(direction: str, mode: str) -> str:
    if direction not in REWRITABLE:
        raise ValueError("unsupported policy direction")
    if mode not in MODES:
        raise ValueError("unsupported policy mode")

    if mode == OBSERVE:
        return ALLOW
    if mode == WARN:
        return REPORT

    if REWRITABLE[direction]:
        return MASK
    return DENY
