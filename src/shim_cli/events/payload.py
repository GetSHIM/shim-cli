from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace as evolve
from html import unescape
from itertools import islice
from typing import Any, Literal

from shim_cli.guard.entities import PLACEHOLDER
from shim_cli.guard.evaluate import line_breaks
from shim_cli.guard.models import Finding

# Matches the hook's own MAX_INPUT_BYTES: anything that got past that refusal
# is worth scanning. Below this, a single large `Read` result was skipped here
# before the detector ever saw it, and reached the model unmasked.
# ponytail: the bound is size, not time. The per-call analysis deadline is the
# backstop; a shared deadline across pieces is the upgrade if this ever bites.
MAX_TEXT_CHARACTERS = 1_000_000
# Below the interpreter's recursion limit with room to spare. The hook has no
# leaf count: its 1 MB input bound already bounds the work, and a count below
# that only turned the fields after it into fields nobody inspected. Anything
# nested deeper than this is not inspected, so the hook withholds it.
MAX_DEPTH = 64

Path = tuple


class PayloadTooLarge(ValueError):
    pass


@dataclass
class Traversal:
    leaves: list = field(default_factory=list)
    characters: int = 0
    partial: bool = False
    skipped: int = 0
    reasons: set[str] = field(default_factory=set)
    max_leaves: int | None = None
    max_characters: int = MAX_TEXT_CHARACTERS
    max_depth: int = MAX_DEPTH

    def add(self, path: Path, text: str) -> None:
        if self.partial and self.characters + len(text) > self.max_characters:
            self.skipped += 1
            self.reasons.add("size-limit")
            return
        self.leaves.append((path, text))
        self.characters += len(text)
        if self.max_leaves is not None and len(self.leaves) > self.max_leaves:
            raise PayloadTooLarge("payload has too many text fields to scan safely")
        if self.characters > self.max_characters:
            raise PayloadTooLarge("payload text exceeds the safe analysis limit")


def walk(
    value: Any,
    root: Path = (),
    *,
    partial: bool = False,
    max_leaves: int | None = None,
    max_characters: int = MAX_TEXT_CHARACTERS,
    max_depth: int = MAX_DEPTH,
) -> Traversal:
    """The defaults bound a hook a user is waiting on; only the proxy changes them."""
    found = Traversal(
        partial=partial,
        max_leaves=max_leaves,
        max_characters=max_characters,
        max_depth=max_depth,
    )
    _walk(value, root, found, 0)
    return found


def _walk(value: Any, path: Path, found: Traversal, depth: int) -> None:
    if depth > found.max_depth:
        if found.partial:
            found.skipped += 1
            found.reasons.add("depth-limit")
            return
        raise PayloadTooLarge("payload is nested more deeply than is safe to scan")
    if isinstance(value, str):
        if value:
            found.add(path, value)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                _walk(item, (*path, key), found, depth + 1)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, (*path, index), found, depth + 1)


def replace(value: Any, replacements: dict) -> Any:
    return _replace(value, (), replacements)


def _replace(value: Any, path: Path, replacements: dict) -> Any:
    if len(path) > MAX_DEPTH:
        return value
    if isinstance(value, str):
        replacement = replacements.get(path)
        if replacement is None:
            return value
        if not isinstance(replacement, str):
            raise TypeError("a string leaf may only be replaced by a string")
        return replacement
    if isinstance(value, dict):
        return {
            key: (
                _replace(item, (*path, key), replacements)
                if isinstance(key, str)
                else item
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _replace(item, (*path, index), replacements)
            for index, item in enumerate(value)
        ]
    return value


def past_deadline(error: BaseException) -> bool:
    # The detector wraps its own timeout in a ValueError; the hook's is bare.
    return isinstance(error, TimeoutError) or isinstance(error.__cause__, TimeoutError)


def _shrink(
    text: str, decision, transforms: tuple, shrink, evaluate, numbering
) -> tuple:
    """What the model will read of `text`, the transforms that made it, and
    what was found in it. Raises only past the deadline.

    A page is converted first and scanned as the model will read it. If that
    cannot be done, the markup is masked and handed on unconverted.
    """
    if "html" in transforms and not decision.partial:
        page = _page(text, decision, shrink, evaluate, numbering)
        if page is not None:
            return page
    kept = tuple(name for name in transforms if name != "html")
    shrunk, names = shrink(_masked(text, decision), kept)
    return shrunk, names, decision


# Conversion keeps private-use characters as text. Three the page holds
# neither as written nor through an entity, put around each value found in
# the markup with its index, show where it lands on the page.
_PRIVATE_USE = range(0xE000, 0xF900)


def _marks(text: str) -> tuple | None:
    held = set(text) | set(unescape(text))
    free = (chr(code) for code in _PRIVATE_USE if chr(code) not in held)
    marks = tuple(islice(free, 3))
    return marks if len(marks) == 3 else None


def _page(text: str, found, shrink, evaluate, numbering: dict) -> tuple | None:
    """The page `text` converts to, masked, or None if it must be masked as
    markup. Raises past the deadline.

    The page is scanned as the model will read it: entities decode, and a
    value cut in two by tags is read whole. Conversion also joins a value to
    the text beside it, where no recognizer finds it, so a value `found` in
    the markup is masked where it lands too, with the placeholder it was
    given there. One that runs across tags has no one place on the page: it
    is masked in the markup, and lands as its placeholder.
    """
    pieces, landed, cursor = [], [], 0
    given = _placeholders(text, found)
    marks = _marks(text) if found.findings else ("", "", "")
    if given is None or marks is None:
        return None
    opening, split, closing = marks
    for index, finding in enumerate(found.findings):
        value = text[finding.start : finding.end]
        after = ""
        if "<" in value or ">" in value:
            value, after = given[index], line_breaks(value)
        pieces += (
            text[cursor : finding.start],
            f"{opening}{index}{split}{value}{closing}{after}",
        )
        cursor = finding.end
    marked, names = shrink("".join(pieces) + text[cursor:], ("html",))
    if not names:
        return None
    page = marked
    if found.findings:
        parts, length, cursor = [], 0, 0
        for match in re.finditer(
            f"{opening}([0-9]+){split}([^{opening}{split}{closing}]*){closing}", marked
        ):
            before = marked[cursor : match.start()]
            start = length + len(before)
            length = start + len(match.group(2))
            parts += (before, match.group(2))
            landed.append((int(match.group(1)), start, length))
            cursor = match.end()
        page = "".join(parts) + marked[cursor:]
        if any(mark in page for mark in marks):
            return None  # a mark without its pair
    try:
        read = evaluate(page, numbering=numbering)
    except (ValueError, TimeoutError) as error:
        if past_deadline(error):
            raise
        return None
    if read.partial:
        return None
    covered = bytearray(len(page))
    for finding in read.findings:
        covered[finding.start : finding.end] = b"\x01" * (finding.end - finding.start)
    joined = [
        (index, start, end) for index, start, end in landed if 0 in covered[start:end]
    ]
    if not joined:
        return _masked(page, read), names, read
    shown = _placeholders(page, read)
    if shown is None:
        return None
    regions = sorted(
        [
            (item.start, item.end, item, shown[at])
            for at, item in enumerate(read.findings)
        ]
        + [(start, end, found.findings[at], given[at]) for at, start, end in joined],
        key=lambda region: (region[0], -region[1]),
    )
    # Overlapping values are masked as one, named by the first.
    merged: list = []
    for region in regions:
        if merged and region[0] < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], region[1]), *merged[-1][2:])
        else:
            merged.append(region)
    pieces, findings, cursor = [], [], 0
    for start, end, item, placeholder in merged:
        pieces += (page[cursor:start], placeholder, line_breaks(page[start:end]))
        findings.append(Finding(item.entity_type, start, end, item.score, item.label))
        cursor = end
    pieces.append(page[cursor:])
    masked = "".join(pieces)
    return masked, names, evolve(read, findings=tuple(findings), redacted_text=masked)


def _placeholders(text: str, decision) -> list | None:
    """The placeholder `decision` wrote for each of its findings, in order,
    or None if it wrote them otherwise: each replaced its value, followed by
    the value's line breaks."""
    written, shift = [], 0
    for finding in decision.findings:
        match = PLACEHOLDER.match(decision.redacted_text, finding.start + shift)
        if match is None:
            return None
        written.append(match.group())
        span = text[finding.start : finding.end]
        shift += len(match.group()) + len(line_breaks(span)) - len(span)
    return written


def _masked(text: str, decision) -> str:
    return decision.redacted_text if decision.findings else text


@dataclass(frozen=True)
class Inspection:
    value: Any
    findings: list
    changed: bool
    transforms: tuple
    markers: tuple
    status: Literal["complete", "partial", "not-inspected"] = "complete"
    skipped: int = 0
    reasons: tuple[str, ...] = ()
    bare_numbers: int = 0


def inspect(
    value: Any,
    evaluate: Callable[..., Any],
    transforms: tuple = (),
    scan_markers: bool = False,
) -> Inspection:
    """`value` is one event. `evaluate(text, numbering=...)` is the detector,
    given one `numbering` for every field of it."""
    found = walk(value, partial=True)
    transform_order = ()
    apply_diet = None
    if transforms:
        from .diet import TRANSFORMS, shrink

        transform_order = TRANSFORMS
        apply_diet = shrink
    marker_order = ()
    scan_injection = None
    if scan_markers:
        from .injection import MARKERS, scan

        marker_order = MARKERS
        scan_injection = scan
    replacements = {}
    findings = []
    applied: set = set()
    markers: set = set()
    inspected = 0
    bare_numbers = 0
    # One numbering for the whole event, so a value keeps its placeholder in
    # every field. Held here only: it maps values to numbers.
    numbering: dict = {}
    for index, (path, text) in enumerate(found.leaves):
        try:
            decision = evaluate(text, numbering=numbering)
        except (ValueError, TimeoutError) as error:
            # Do not consume the hook deadline and continue.
            if past_deadline(error):
                found.skipped += len(found.leaves) - index
                found.reasons.add("deadline")
                break
            found.skipped += 1
            found.reasons.add("analysis-failed")
            continue
        inspected += 1
        bare_numbers += decision.bare_numbers
        if decision.partial:
            # A piece of an oversized leaf failed; the rest is masked below.
            found.skipped += 1
            found.reasons.add("piece-failed")
        current = decision.redacted_text if decision.findings else text
        late = False
        if apply_diet is not None:
            try:
                current, names, decision = _shrink(
                    text, decision, transforms, apply_diet, evaluate, numbering
                )
                applied.update(names)
            except (ValueError, TimeoutError):
                late = True  # past the deadline: this leaf is masked, not shrunk
        if scan_injection is not None:  # what the model will read
            markers.update(scan_injection(current))
        if decision.findings:
            findings.append((path, decision))
        if current != text:
            replacements[path] = current
        if late:
            found.skipped += len(found.leaves) - index - 1
            found.reasons.add("deadline")
            break
    ordered_transforms = tuple(name for name in transform_order if name in applied)
    ordered_markers = tuple(name for name in marker_order if name in markers)
    status = "complete"
    if found.skipped:
        status = "partial" if inspected else "not-inspected"
        # Part of it was never read, so no version of it may be handed on.
        value, replacements = None, {}
    return Inspection(
        replace(value, replacements) if replacements else value,
        findings,
        bool(replacements),
        ordered_transforms,
        ordered_markers,
        status,
        found.skipped,
        tuple(sorted(found.reasons)),
        bare_numbers,
    )


def mask(value: Any, evaluate: Callable[..., Any]) -> tuple:
    result = inspect(value, evaluate)
    return result.value, result.findings, result.changed
