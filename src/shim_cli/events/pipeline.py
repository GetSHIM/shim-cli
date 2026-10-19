from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from shim_cli import json_bytes
from shim_cli.policy import (
    ALLOW,
    DENY,
    EXECUTABLE_TEXT,
    INBOUND,
    LOCAL_WRITE,
    MASK,
    MCP,
    OBSERVE,
    OUTBOUND,
    REPORT,
    decide,
    direction_for,
    shim_control,
    tool_kind,
)
from shim_cli.session.record import (
    NOT_INSPECTED,
    UNKNOWN_TOOL_LABEL,
    Record,
    display_label,
    scrubbed_target,
)

from .payload import PayloadTooLarge, inspect, past_deadline


@dataclass(frozen=True)
class Event:
    __slots__ = ("tool", "payload", "target")

    tool: str
    payload: object
    target: str


# What a client's hook can actually do at one event, measured against the
# running client rather than taken from its documentation.
REWRITE = "rewrite"  # the payload the model reads can be replaced
REFUSE = "refuse"  # it cannot be changed, but the call can be stopped
REPORT_ONLY = "report-only"  # the model already has it; only the user can be told
POWERS = (REWRITE, REFUSE, REPORT_ONLY)


@dataclass(frozen=True)
class Adapter:
    __slots__ = ("client", "event", "root", "decode", "encode", "power")

    client: str
    event: str
    root: str
    decode: Callable[[bytes | dict[str, object]], Event]
    # (action, payload, message, found, tool label[, tool name]): the name is
    # passed only for UNINSPECTED, where what can replace a result depends on
    # which tool it came from and a label may no longer say.
    encode: Callable[..., bytes]
    # No default: a new adapter must say which of the three it is, because
    # claiming more than the client grants is how a user ends up trusting a
    # block that never happened. (A default would also clash with __slots__,
    # which the 3.9 archive still needs.)
    power: str


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


def _custom_counts(findings) -> tuple:
    totals: dict = {}
    for _path, decision in findings:
        for name, count in decision.custom_counts:
            totals[name] = totals.get(name, 0) + count
    return tuple(sorted(totals.items()))


def _summary(counts) -> str:
    return ", ".join(f"{entity} ({count})" for entity, count in counts)


# Why an event was not inspected, as the user is told it. None of these is
# drawn from the payload.
DEADLINE = "deadline"
TOO_LARGE = "too large"
SETTINGS_UNREADABLE = "settings unreadable"
ANALYSIS_FAILED = "analysis failed"
# Not a policy action. The event could not be inspected, so it fails closed in
# every mode, as a prompt does: each encoder withholds, refuses, or — where
# its client can do neither — tells the user and the model.
UNINSPECTED = "uninspected"
UNINSPECTED_CALL = (
    "shim: could not inspect this {tool} call ({reason}), so it was not allowed."
)


def failure_reason(error: BaseException) -> str:
    if past_deadline(error):
        return DEADLINE
    if isinstance(error, (RecursionError, PayloadTooLarge)):
        return TOO_LARGE
    return ANALYSIS_FAILED


def _skipped_reason(reasons: tuple) -> str:
    if "deadline" in reasons:
        return DEADLINE
    if "size-limit" in reasons or "depth-limit" in reasons:
        return TOO_LARGE
    return ANALYSIS_FAILED


def uninspected_report(tool: str, reason: str, found: str) -> tuple:
    """The user's line and the model's, for output the model already has."""
    system = (
        f"shim: could not inspect this {tool} output ({reason}); the model has "
        "it as it was."
    )
    if found:
        system += f" Before it stopped, shim found {found}."
    return system, (
        f"shim could not inspect this {tool} output ({reason}). Do not repeat "
        "values from it in replies, files or commands."
    )


def _size(value) -> int:
    return len(json_bytes.dumps(value))


_UNROUTED = (
    " shim cannot tell where this tool sends its input, so it refuses the call "
    "rather than rewrite it. Leave these values out, or ask the user how to "
    "continue."
)
_RESHAPED = (
    "shim: reformatted this result to save space ({}); it is not byte-for-byte "
    "what the tool returned."
)


def _message(tool: str, counts: tuple, action: str) -> str:
    what = _summary(counts)
    where = f" in {tool}" if tool else ""
    if action == MASK:
        return f"shim: masked {what}{where}."
    if action == DENY:
        return f"shim: blocked {what}{where}."
    return f"shim: found {what}{where}. Not modified."


# A call refused before inspection, in every mode, because it could turn shim
# off. Neither reason quotes the call: only the name of the file it would
# change.
_SELF_PROTECTION = "self-protection"
_SETTINGS_REFUSAL = (
    "shim: refused this change to {name}, which holds shim's settings or hooks; "
    "an agent that can change it can turn shim off. To change it, edit it "
    "yourself in your own terminal."
)
_COMMAND_REFUSAL = (
    "shim: refused `shim {subcommand}`; an agent that can run it can turn shim "
    "off. To run it, run it yourself in your own terminal."
)
# Where Claude Code's and VS Code's write tools name a file, and the lines of
# an apply_patch patch that do.
_PATH_KEYS = ("file_path", "filePath", "notebook_path", "notebookPath", "path")
_PATCH_HEADERS = (
    "*** Add File:",
    "*** Update File:",
    "*** Delete File:",
    "*** Move to:",
)


def _written(tool: str, body: dict) -> list:
    """Every path a write tool's input names, its edit lists' included."""
    entries = [body]
    for value in body.values():
        if isinstance(value, list):
            entries.extend(value)
    paths = [
        entry[key]
        for entry in entries
        if isinstance(entry, dict)
        for key in _PATH_KEYS
        if isinstance(entry.get(key), str)
    ]
    if tool == "apply_patch":
        for value in body.values():
            if not isinstance(value, str):
                continue
            for line in value.splitlines():
                line = line.strip()
                if line.startswith(_PATCH_HEADERS):
                    paths.append(line.split(":", 1)[1].strip())
    return paths


def _task_text(value):
    """A tasks.json command or argument's text: a string, or lines joined."""
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return " ".join(value)
    return value if isinstance(value, str) else None


def _task_word(value):
    """A VS Code task's command or argument as VS Code writes it into a bash
    or zsh command line, or None when it has no text.

    A string is wrapped in single quotes only when it holds a space outside
    quotes and is not already wrapped; `{value, quoting}` is escaped, or
    wrapped in double or, by default, single quotes. Nothing inside a
    wrapped word is escaped: `a "` becomes `'a "'`, and a `;` after it runs.
    """
    if isinstance(value, dict):
        text = _task_text(value.get("value"))
        quoting = value.get("quoting")
        quoting = quoting.lower() if isinstance(quoting, str) else ""
        if not text:
            return None
        if quoting == "escape":
            return "".join("\\" + char if char in " \"'" else char for char in text)
        return f'"{text}"' if quoting == "weak" else f"'{text}'"
    text = _task_text(value)
    if text is None or (len(text) > 1 and text[0] in "'\"" and text[-1] == text[0]):
        return text
    quote = ""
    for char in text:
        if char == quote:
            quote = ""
        elif not quote and char in "'\"":
            quote = char
        elif not quote and char == " ":
            return f"'{text}'"
    return text


def _command_lines(value) -> list:
    """Every command line a shell tool's input runs: its `command`, and a VS
    Code task's `command` and `args`, and its per-platform overrides, as VS
    Code writes them for the shell. A command without arguments runs as given.
    """
    if not isinstance(value, dict):
        return []
    lines = []
    command, arguments = value.get("command"), value.get("args")
    if (text := _task_text(command)) is not None and not arguments:
        lines.append(text)
    elif (program := _task_word(command)) is not None:
        words = map(_task_word, arguments if isinstance(arguments, list) else ())
        lines.append(" ".join((program, *(word for word in words if word is not None))))
    for nested in value.values():
        lines.extend(_command_lines(nested))
    return lines


def _self_protection(direction: str, tool: str, body, protected) -> str:
    """Why a call is refused before it is inspected, or "" to inspect it.

    A shell command running `shim config`, `revert` or `uninstall`, or a write
    to a file `protected` names, can turn shim off from inside the agent it
    guards. `protected` maps a path as the tool gives it to the file's name,
    or "".
    """
    if not isinstance(body, dict):
        return ""
    if direction == EXECUTABLE_TEXT:
        for line in _command_lines(body):
            if subcommand := shim_control(line):
                return _COMMAND_REFUSAL.format(subcommand=subcommand)
    if direction == LOCAL_WRITE and protected is not None:
        for path in dict.fromkeys(_written(tool, body)):
            if name := protected(path):
                return _SETTINGS_REFUSAL.format(name=name)
    return ""


def _decide(entry: Adapter, direction: str, mode: str, unrouted: bool) -> str:
    """The strongest action this event can honestly deliver.

    Enforcement is capped by what the client grants, never by what was asked
    for: reporting a mask that did not happen, or a block the model read
    straight through, is worse than reporting the finding plainly. A call
    whose destination shim does not know is refused rather than rewritten.
    """
    action = decide(direction, mode)
    if entry.power == REPORT_ONLY and action in (MASK, DENY):
        return REPORT
    if action == MASK and (entry.power == REFUSE or unrouted):
        return DENY
    return action


def process(
    entry: Adapter,
    raw: bytes | dict[str, object],
    mode_for,
    evaluate,
    diet: tuple = (),
    entities_for=None,
    protected=None,
) -> Outcome:
    event = entry.decode(raw)
    tool = event.tool

    direction = direction_for(entry.event, tool, entry.client)
    kind = tool_kind(entry.client, tool)
    # An MCP or unknown tool's input may go anywhere: never rewritten.
    unrouted = direction == OUTBOUND and kind != OUTBOUND
    mode = mode_for(direction, tool)
    target = scrubbed_target(event.target, evaluate)
    tool_label = display_label(tool, UNKNOWN_TOOL_LABEL)
    if tool_label != UNKNOWN_TOOL_LABEL:
        decision = evaluate(tool_label)
        if decision.counts:
            tool_label = display_label(decision.redacted_text, UNKNOWN_TOOL_LABEL)
    if entities_for is not None:
        scoped = entities_for(tool, entry.event)
        original = evaluate

        def evaluate(text, numbering=None, _entities=scoped):
            return original(text, _entities, numbering=numbering)

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
        custom=(),
        bare_numbers=0,
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
            custom=custom,
            bare_numbers=bare_numbers,
        )

    if body is None:
        return Outcome(b"", record(ALLOW, note="no payload at this key"))

    refusal = _self_protection(direction, tool, body, protected)
    if refusal:
        return Outcome(
            entry.encode(DENY, body, refusal, "", tool_label),
            record(DENY, note=_SELF_PROTECTION),
        )

    inbound = direction == INBOUND
    # Only what came over a network or from an MCP server is reshaped: a local
    # tool's or a command's output may be a file the model goes on to edit.
    shrinkable = (
        inbound
        and kind in (OUTBOUND, MCP)
        and mode != OBSERVE
        and entry.power == REWRITE
    )
    transforms = diet if shrinkable else ()
    result = inspect(body, evaluate, transforms, scan_markers=inbound)
    if result.skipped:
        reason = _skipped_reason(result.reasons)
        counts = _counts(result.findings)
        return Outcome(
            entry.encode(
                UNINSPECTED,
                body,
                reason,
                _summary(counts),
                tool_label,
                tool,
            ),
            record(
                REPORT if entry.power == REPORT_ONLY else DENY,
                counts,
                fields=len(result.findings),
                # As the hook notes an event it could not read at all: read
                # in part or not at all, it is withheld, refused or reported.
                note=f"{NOT_INSPECTED}: {reason}",
                markers=result.markers,
                custom=_custom_counts(result.findings),
                bare_numbers=result.bare_numbers,
            ),
        )

    rewritten, findings, changed = result.value, result.findings, result.changed

    if not findings:
        if not changed:
            return Outcome(
                b"",
                record(
                    ALLOW,
                    fields=0,
                    markers=result.markers,
                    bare_numbers=result.bare_numbers,
                ),
            )
        return Outcome(
            entry.encode(
                MASK,
                rewritten,
                _RESHAPED.format(", ".join(result.transforms)),
                "",
                tool_label,
            ),
            record(
                ALLOW,
                out_bytes=_size(rewritten),
                transforms=result.transforms,
                markers=result.markers,
                bare_numbers=result.bare_numbers,
            ),
        )

    counts = _counts(findings)
    action = _decide(entry, direction, mode, unrouted)
    if action == ALLOW:
        return Outcome(
            b"",
            record(
                ALLOW,
                counts,
                fields=len(findings),
                markers=result.markers,
                custom=_custom_counts(findings),
                bare_numbers=result.bare_numbers,
            ),
        )

    message = _message(tool_label, counts, action)
    if action == DENY and unrouted:
        message += _UNROUTED
    if action == MASK and result.transforms:
        message = f"{_RESHAPED.format(', '.join(result.transforms))} {message}"
    emitted = rewritten if action == MASK and changed else body
    output = entry.encode(action, emitted, message, _summary(counts), tool_label)
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
            custom=_custom_counts(findings),
            bare_numbers=result.bare_numbers,
        ),
    )


__all__ = [
    "ANALYSIS_FAILED",
    "DEADLINE",
    "SETTINGS_UNREADABLE",
    "TOO_LARGE",
    "UNINSPECTED",
    "UNINSPECTED_CALL",
    "Adapter",
    "Event",
    "Outcome",
    "failure_reason",
    "process",
    "uninspected_report",
]
