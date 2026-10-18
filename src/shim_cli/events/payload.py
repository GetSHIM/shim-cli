from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace as evolve
from typing import Any, Literal

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


def _shrink(masked: str, decision, transforms: tuple, shrink, evaluate) -> tuple:
    """`masked` shrunk, and `decision` widened by what the shrinking revealed.

    Converting a page decodes entities and closes up text split across tags,
    so the text the model will read can hold a value the first pass could not
    see. It is scanned again, and left unconverted if that scan fails.
    Raises only past the deadline.
    """
    # ponytail: the second scan numbers its placeholders from 1 again, so
    # `<EMAIL_1>` can name two values on one page; offset them if that misleads.
    shrunk, names = shrink(masked, transforms)
    if "html" not in names:
        return shrunk, names, decision
    try:
        again = evaluate(shrunk)
    except (ValueError, TimeoutError) as error:
        if past_deadline(error):
            raise
        return masked, (), decision
    if again.partial:
        return masked, (), decision
    if not again.findings:
        return shrunk, names, decision
    widened = evolve(
        decision,
        findings=decision.findings + again.findings,
        redacted_text=again.redacted_text,
    )
    return again.redacted_text, names, widened


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
    evaluate: Callable[[str], Any],
    transforms: tuple = (),
    scan_markers: bool = False,
) -> Inspection:
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
    for index, (path, text) in enumerate(found.leaves):
        try:
            decision = evaluate(text)
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
                    current, decision, transforms, apply_diet, evaluate
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


def mask(value: Any, evaluate: Callable[[str], Any]) -> tuple:
    result = inspect(value, evaluate)
    return result.value, result.findings, result.changed
