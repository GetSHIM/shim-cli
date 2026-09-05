from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

MAX_TEXT_CHARACTERS = 200_000
MAX_DEPTH = 24
MAX_LEAVES = 2_000

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

    def add(self, path: Path, text: str) -> None:
        if self.partial and (
            len(self.leaves) >= MAX_LEAVES
            or self.characters + len(text) > MAX_TEXT_CHARACTERS
        ):
            self.skipped += 1
            self.reasons.add("size-limit")
            return
        self.leaves.append((path, text))
        self.characters += len(text)
        if len(self.leaves) > MAX_LEAVES:
            raise PayloadTooLarge("payload has too many text fields to scan safely")
        if self.characters > MAX_TEXT_CHARACTERS:
            raise PayloadTooLarge("payload text exceeds the safe analysis limit")


def walk(value: Any, root: Path = (), *, partial: bool = False) -> Traversal:
    found = Traversal(partial=partial)
    _walk(value, root, found, 0)
    return found


def _walk(value: Any, path: Path, found: Traversal, depth: int) -> None:
    if depth > MAX_DEPTH:
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
    for index, (path, text) in enumerate(found.leaves):
        try:
            decision = evaluate(text)
        except (ValueError, TimeoutError) as error:
            # The detector wraps timeouts; do not consume the hook deadline and continue.
            if isinstance(error, TimeoutError) or isinstance(
                error.__cause__, TimeoutError
            ):
                found.skipped += len(found.leaves) - index
                found.reasons.add("deadline")
                break
            found.skipped += 1
            found.reasons.add("analysis-failed")
            continue
        inspected += 1
        current = text
        if decision.findings:
            findings.append((path, decision))
            current = decision.redacted_text
        if scan_injection is not None:
            markers.update(scan_injection(current))
        if apply_diet is not None:
            current, names = apply_diet(current, transforms)
            applied.update(names)
        if current != text:
            replacements[path] = current
    ordered_transforms = tuple(name for name in transform_order if name in applied)
    ordered_markers = tuple(name for name in marker_order if name in markers)
    status = "complete"
    if found.skipped:
        status = "partial" if inspected else "not-inspected"
    return Inspection(
        replace(value, replacements) if replacements else value,
        findings,
        bool(replacements),
        ordered_transforms,
        ordered_markers,
        status,
        found.skipped,
        tuple(sorted(found.reasons)),
    )


def mask(value: Any, evaluate: Callable[[str], Any]) -> tuple:
    result = inspect(value, evaluate)
    return result.value, result.findings, result.changed
