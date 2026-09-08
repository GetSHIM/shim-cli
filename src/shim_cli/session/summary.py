from __future__ import annotations

import math
from statistics import median

ACTION_LABELS = (
    ("mask", "masked"),
    ("deny", "blocked"),
    ("report", "warned"),
)
MAX_SOURCES = 3
BYTES_PER_TOKEN = 4
MODEL_OUTPUT = "model-output"
NOT_LEAKS = "model-generated content, not leaks"


def _sources(records: list) -> list:
    order: dict = {}
    for record in records:
        tool = record.get("tool_name")
        target = record.get("target")
        tool = tool if isinstance(tool, str) else ""
        target = target if isinstance(target, str) else ""
        if tool:
            where = f"{tool} {_basename(target)}" if target else tool
        else:
            event = record.get("event")
            where = _event_label(event if isinstance(event, str) else "")
        if where:
            order[where] = order.get(where, 0) + 1
    return [name for name, _count in sorted(order.items(), key=lambda p: (-p[1], p[0]))]


def _basename(target: str) -> str:
    if "://" in target:
        return target
    return target.rsplit("/", 1)[-1] or target


def _event_label(event: str) -> str:
    if event in ("UserPromptSubmit", "userPromptTransformed"):
        return "your prompt"
    return event


def _totals(records: list) -> list:
    counts: dict = {}
    for record in records:
        entities = record.get("entities")
        if not isinstance(entities, dict):
            continue
        for entity, count in entities.items():
            if isinstance(entity, str) and isinstance(count, int):
                counts[entity] = counts.get(entity, 0) + count
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))


def _overhead(records: list) -> tuple:
    latencies = []
    for record in records:
        latency = record.get("latency_ms")
        if isinstance(latency, bool) or not isinstance(latency, (int, float)):
            continue
        try:
            if latency >= 0 and math.isfinite(latency):
                latencies.append(latency)
        except OverflowError:
            continue
    latencies.sort()
    if not latencies:
        return (0, 0)
    middle = median(latencies)
    index = round(0.95 * (len(latencies) - 1))
    return (round(middle), round(latencies[index]))


def _saved(records: list) -> int:
    total = 0
    for record in records:
        if not record.get("transforms"):
            continue
        before = record.get("in_bytes")
        after = record.get("out_bytes")
        if isinstance(before, int) and isinstance(after, int) and before > after:
            total += before - after
    return total


def _marker_totals(records: list) -> list:
    counts: dict = {}
    for record in records:
        markers = record.get("markers")
        if not isinstance(markers, list):
            continue
        for marker in markers:
            if isinstance(marker, str):
                counts[marker] = counts.get(marker, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))


def _where(records: list) -> str:
    sources = _sources(records)
    if not sources:
        return ""
    shown = ", ".join(sources[:MAX_SOURCES])
    if len(sources) > MAX_SOURCES:
        shown += f", +{len(sources) - MAX_SOURCES} more"
    return f"  ({shown})"


def _carrying(records: list, marker: str) -> list:
    return [
        record
        for record in records
        if isinstance(record.get("markers"), list) and marker in record["markers"]
    ]


def _uninspected(records: list) -> list:
    from .record import NOT_INSPECTED

    return [
        record
        for record in records
        if isinstance(record.get("note"), str)
        and record["note"].startswith((NOT_INSPECTED, "not-inspected:"))
    ]


def _custom_totals(records: list) -> list:
    counts: dict = {}
    for record in records:
        custom = record.get("custom")
        if not isinstance(custom, dict):
            continue
        for name, count in custom.items():
            if isinstance(name, str) and isinstance(count, int):
                counts[name] = counts.get(name, 0) + count
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))


def _acted(records: list) -> list:
    return [
        record
        for record in records
        if record.get("action") not in (None, "allow")
        and record.get("direction") != MODEL_OUTPUT
    ]


def _model_output(records: list) -> list:
    return [
        record
        for record in records
        if record.get("direction") == MODEL_OUTPUT and record.get("entities")
    ]


def render(records: list, capped: bool = False) -> str:
    acted = _acted(records)
    saved = _saved(records)
    markers = _marker_totals(records)
    skipped = _uninspected(records)
    partial = [
        record
        for record in records
        if str(record.get("note", "")).startswith("partial:")
    ]
    lines: list = []
    for action, label in ACTION_LABELS:
        matching = [record for record in acted if record.get("action") == action]
        if not matching:
            continue
        first = True
        for entity, count in _totals(matching):
            relevant = [
                record
                for record in matching
                if isinstance(record.get("entities"), dict)
                and entity in record["entities"]
            ]
            column = label if first else " " * len(label)
            first = False
            lines.append(f"  {column:<9} {count} {entity}{_where(relevant)}")
    replies = _model_output(records)
    # The `custom` line sits under `masked` and has to agree with it. Counting
    # the model's own replies here made it read 4 under a `masked 2 CUSTOM`.
    named = _custom_totals(
        [record for record in records if record.get("direction") != MODEL_OUTPUT]
    )
    if named:
        listed = ", ".join(f"{count} {name}" for name, count in named)
        lines.append(f"  {'custom':<9} {listed}")
    if replies:
        listed = ", ".join(f"{count} {entity}" for entity, count in _totals(replies))
        by_name = _custom_totals(replies)
        if by_name:
            listed += " (" + ", ".join(f"{c} {n}" for n, c in by_name) + ")"
        lines.append(f"  {'model':<9} {listed} in its replies ({NOT_LEAKS})")
    first = True
    for marker, count in markers:
        column = "flagged" if first else " " * len("flagged")
        first = False
        lines.append(
            f"  {column:<9} {count} {marker}{_where(_carrying(records, marker))}"
        )
    if skipped:
        lines.append(
            f"  {'skipped':<9} {len(skipped)} not inspected, passed through"
            f"{_where(skipped)}"
        )
    if partial:
        lines.append(
            f"  incomplete inspection of {len(partial)} tool event(s){_where(partial)}"
        )
    if saved:
        lines.append(
            f"  {'shrank':<9} {saved} bytes of tool results "
            f"(~{saved // BYTES_PER_TOKEN} tokens)"
        )
    if not lines:
        return ""
    median, p95 = _overhead(records)
    lines.append(f"  {'overhead':<9} {median} ms median, {p95} ms p95")
    if capped:
        lines.append(
            "  (session log reached its size cap; later events are not counted)"
        )
    return "\n".join(["shim — this session", *lines])


def as_json(records: list, capped: bool = False) -> dict:
    acted = _acted(records)
    median, p95 = _overhead(records)
    actions: dict = {}
    for action, _label in ACTION_LABELS:
        matching = [record for record in acted if record.get("action") == action]
        if matching:
            actions[action] = {
                "entities": dict(_totals(matching)),
                "sources": _sources(matching),
                "events": len(matching),
            }
    saved = _saved(records)
    return {
        "events": len(records),
        "acted": len(acted),
        "actions": actions,
        "overhead_ms": {"median": median, "p95": p95},
        "bytes_saved": saved,
        "tokens_saved_approx": saved // BYTES_PER_TOKEN,
        "not_inspected": len(_uninspected(records)),
        "incomplete": sum(
            str(record.get("note", "")).startswith(("partial:", "not-inspected:"))
            for record in records
        ),
        "markers": {
            marker: {"count": count, "sources": _sources(_carrying(records, marker))}
            for marker, count in _marker_totals(records)
        },
        "model_output": dict(_totals(_model_output(records))),
        "custom": dict(_custom_totals(records)),
        "capped": capped,
    }


__all__ = ["as_json", "render"]
