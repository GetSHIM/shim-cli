from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from shim_guard.policy import (
    ALLOW,
    DENY,
    INBOUND,
    MASK,
    OBSERVE,
    REPORT,
    decide,
    direction_for,
)
from shim_guard.session.record import (
    UNKNOWN_TOOL_LABEL,
    Record,
    display_label,
)

from .payload import inspect


@dataclass(frozen=True)
class Event:
    __slots__ = ("tool", "payload", "target", "views_file")

    tool: str
    payload: object
    target: str
    views_file: bool


@dataclass(frozen=True)
class Adapter:
    __slots__ = ("client", "event", "root", "decode", "encode")

    client: str
    event: str
    root: str
    decode: Callable[[bytes | dict[str, object]], Event]
    encode: Callable[[str, object, str], bytes]


@dataclass(frozen=True)
class Outcome:
    __slots__ = ("output", "record")

    output: bytes
    record: Record


def _counts(findings) -> tuple:
    totals: dict = {}
    for _path, decision in findings:
        for entity, count in decision.counts:
            totals[entity] = totals.get(entity, 0) + count
    return tuple(sorted(totals.items()))


def _summary(counts) -> str:
    return ", ".join(f"{entity} ({count})" for entity, count in counts)


MAX_TARGET_CHARS = 120
MAX_TARGET_SCAN_CHARS = 512


def _target(value: str, evaluate) -> str:
    if not value:
        return ""
    scanned = value[-MAX_TARGET_SCAN_CHARS:]
    decision = evaluate(scanned)
    text = _printable(decision.redacted_text if decision.counts else scanned)
    if len(text) <= MAX_TARGET_CHARS:
        return text
    return "…" + text[-MAX_TARGET_CHARS:]


def _printable(text: str) -> str:
    return "".join(character for character in text if character.isprintable())


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


def _message(tool: str, counts: tuple, action: str) -> str:
    what = _summary(counts)
    where = f" in {tool}" if tool else ""
    if action == MASK:
        return f"shim: masked {what}{where}."
    if action == DENY:
        return f"shim: blocked {what}{where}."
    return f"shim: found {what}{where}. Not modified."


def process(
    entry: Adapter,
    raw: bytes | dict[str, object],
    mode_for,
    evaluate,
    diet: tuple = (),
    entities_for=None,
) -> Outcome:
    event = entry.decode(raw)
    tool = event.tool

    direction = direction_for(entry.event, tool)
    mode = mode_for(direction, tool)
    target = _target(event.target, evaluate)
    tool_label = display_label(tool, UNKNOWN_TOOL_LABEL)
    if tool_label != UNKNOWN_TOOL_LABEL:
        decision = evaluate(tool_label)
        if decision.counts:
            tool_label = display_label(decision.redacted_text, UNKNOWN_TOOL_LABEL)
    if entities_for is not None:
        scoped = entities_for(tool, entry.event)
        original = evaluate

        def evaluate(text, _entities=scoped):
            return original(text, _entities)

    body = event.payload
    in_bytes = _size(body) if body else 0

    def record(
        action,
        counts=(),
        out_bytes=0,
        fields=0,
        note="",
        transforms=(),
        markers=(),
    ) -> Record:
        return Record(
            client=entry.client,
            event=entry.event,
            tool_name=tool_label,
            target=target,
            direction=direction,
            mode=mode,
            action=action,
            entities=counts,
            in_bytes=in_bytes,
            out_bytes=out_bytes,
            fields=fields,
            note=note,
            transforms=transforms,
            markers=markers,
        )

    if body is None:
        return Outcome(b"", record(ALLOW, note="no payload at this key"))

    inbound = direction == INBOUND
    shrinkable = inbound and mode != OBSERVE and not event.views_file
    transforms = diet if shrinkable else ()
    result = inspect(body, evaluate, transforms, scan_markers=inbound)
    if result.skipped:
        note = f"{result.status}: {result.skipped} fields or subtrees not inspected ({', '.join(result.reasons)})"
        counts = _counts(result.findings)
        action = decide(direction, mode) if counts else ALLOW
        can_rewrite = action == MASK or (not counts and shrinkable and result.changed)
        message = "shim: inspection incomplete; uninspected content was not modified."
        if counts:
            message = _message(tool_label, counts, action) + " " + message
        emitted = result.value if can_rewrite else body
        return Outcome(
            entry.encode(
                MASK if can_rewrite else DENY if action == DENY else REPORT,
                emitted,
                message,
            ),
            record(
                action,
                counts,
                out_bytes=_size(emitted),
                fields=len(result.findings),
                note=note,
                transforms=result.transforms if can_rewrite else (),
                markers=result.markers,
            ),
        )

    rewritten, findings, changed = result.value, result.findings, result.changed

    if not findings:
        if not changed:
            return Outcome(b"", record(ALLOW, fields=0, markers=result.markers))
        return Outcome(
            entry.encode(MASK, rewritten, ""),
            record(
                ALLOW,
                out_bytes=_size(rewritten),
                transforms=result.transforms,
                markers=result.markers,
            ),
        )

    counts = _counts(findings)
    action = decide(direction, mode)
    if action == ALLOW:
        return Outcome(b"", record(ALLOW, counts, fields=len(findings)))

    message = _message(tool_label, counts, action)
    emitted = rewritten if action == MASK and changed else body
    output = entry.encode(action, emitted, message if action != MASK else "")
    out_bytes = _size(emitted) if action == MASK else in_bytes
    return Outcome(
        output,
        record(
            action,
            counts,
            out_bytes=out_bytes,
            fields=len(findings),
            transforms=result.transforms if action == MASK else (),
            markers=result.markers,
        ),
    )


__all__ = ["Adapter", "Event", "Outcome", "process"]
