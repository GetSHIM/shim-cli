from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class StateKind(str, Enum):
    ABSENT = "absent"
    FILE = "file"
    UNSAFE = "unsafe"


class Action(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    NOOP = "noop"
    CONFLICT = "conflict"
    REFUSE = "refuse"


@dataclass(frozen=True)
class FileState:
    kind: StateKind
    path: Path
    content: bytes | None = None
    parent_device: int | None = None
    parent_inode: int | None = None
    fingerprint: tuple[int, ...] | None = None
    mode: int | None = None
    max_bytes: int = 1_000_000
    reason: str = ""


@dataclass(frozen=True)
class Plan:
    target: Path
    state: FileState
    expected: bytes | None
    action: Action
    message: str


def plan_change(
    target: Path,
    state: FileState,
    expected: bytes | None,
    conflict: str = "",
) -> Plan:
    target = Path(target)
    if state.path != target:
        action, message = Action.REFUSE, "state was inspected for a different path"
    elif state.kind is StateKind.UNSAFE:
        action, message = Action.REFUSE, state.reason or "unsafe target"
    elif (
        state.kind is StateKind.ABSENT
        and expected is not None
        and None in (state.parent_device, state.parent_inode)
    ):
        action, message = Action.REFUSE, state.reason or "target parent is absent"
    elif expected is not None and len(expected) > state.max_bytes:
        action, message = Action.REFUSE, "replacement exceeds the inspection limit"
    elif conflict:
        action, message = Action.CONFLICT, conflict
    elif expected is None:
        action, message = (
            (Action.NOOP, "no file change is needed")
            if state.kind is StateKind.ABSENT
            else (Action.CONFLICT, "deletion is not supported")
        )
    elif state.kind is StateKind.ABSENT:
        action, message = Action.CREATE, "create target"
    elif state.content == expected:
        action, message = Action.NOOP, "target already matches"
    else:
        action, message = Action.UPDATE, "update target"
    return Plan(target, state, expected, action, message)
