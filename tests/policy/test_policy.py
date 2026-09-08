from __future__ import annotations

import pytest

from shim_cli import policy
from shim_cli.policy import (
    ALLOW,
    DENY,
    ENFORCE,
    EXECUTABLE_TEXT,
    INBOUND,
    LOCAL_WRITE,
    MASK,
    OBSERVE,
    OUTBOUND,
    REPORT,
    USER_PROMPT,
    WARN,
    decide,
    direction_for,
)


@pytest.mark.parametrize(
    ("event", "tool", "expected"),
    (
        ("UserPromptSubmit", "", USER_PROMPT),
        ("userPromptTransformed", "", USER_PROMPT),
        ("PreToolUse", "Bash", EXECUTABLE_TEXT),
        ("PreToolUse", "Shell", EXECUTABLE_TEXT),
        ("PreToolUse", "Write", LOCAL_WRITE),
        ("PreToolUse", "Edit", LOCAL_WRITE),
        ("PreToolUse", "apply_patch", LOCAL_WRITE),
        ("PreToolUse", "NotebookEdit", LOCAL_WRITE),
        ("PreToolUse", "WebFetch", OUTBOUND),
        ("PreToolUse", "mcp__server__tool", OUTBOUND),
        ("preToolUse", "Read", OUTBOUND),
        ("PostToolUse", "Read", INBOUND),
        ("PostToolUse", "Bash", INBOUND),
        ("PostToolUse", "Write", INBOUND),
        ("PostToolUseFailure", "Bash", INBOUND),
        ("postToolUse", "mcp__server__tool", INBOUND),
    ),
)
def test_direction_is_decided_by_payload_kind_not_tool_name(
    event: str, tool: str, expected: str
) -> None:
    assert direction_for(event, tool) == expected


def test_unknown_events_are_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="unsupported hook event"):
        direction_for("SessionStart", "Read")


@pytest.mark.parametrize("direction", (LOCAL_WRITE, EXECUTABLE_TEXT, USER_PROMPT))
def test_unrewritable_directions_are_never_masked(direction: str) -> None:
    assert policy.REWRITABLE[direction] is False
    for mode in (OBSERVE, WARN, ENFORCE):
        assert decide(direction, mode) != MASK


@pytest.mark.parametrize("direction", (OUTBOUND, INBOUND))
def test_rewritable_directions_mask_only_under_enforce(direction: str) -> None:
    assert decide(direction, OBSERVE) == ALLOW
    assert decide(direction, WARN) == REPORT
    assert decide(direction, ENFORCE) == MASK


@pytest.mark.parametrize("direction", (LOCAL_WRITE, EXECUTABLE_TEXT, USER_PROMPT))
def test_unrewritable_directions_deny_under_enforce(direction: str) -> None:
    assert decide(direction, ENFORCE) == DENY


def test_observe_never_acts() -> None:
    for direction in policy.DIRECTIONS:
        assert decide(direction, OBSERVE) == ALLOW


def test_unknown_directions_and_modes_are_refused() -> None:
    with pytest.raises(ValueError, match="unsupported policy direction"):
        decide("sideways", WARN)
    with pytest.raises(ValueError, match="unsupported policy mode"):
        decide(INBOUND, "paranoid")
