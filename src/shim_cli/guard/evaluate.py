from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

from .analyze import analyze
from .entities import ENTITY_TYPES, MAX_REVEAL_DIGITS
from .models import Finding, GuardDecision
from .normalize import MAX_SOURCE_CHARACTERS

# The one definition of what a placeholder looks like, in both forms.
PLACEHOLDER = re.compile(rf"<[A-Z_]+_[0-9]+(?::[0-9]{{1,{MAX_REVEAL_DIGITS}}})?>")


def _tail(span: str, digits: int) -> str:
    """The last few digits, ignoring the separators the value was printed with."""
    return "".join(character for character in span if character.isdigit())[-digits:]


def _pieces(text: str) -> Iterator[tuple[int, str]]:
    """Cut on the last newline before each limit, so no line is split.

    A value that spans a line break is the residual risk, recorded in
    `docs/privacy.md`. Overlapping the pieces would close it at the cost of
    de-duplicating findings in the overlap; the newline split is the default.
    """
    start = 0
    while start < len(text):
        end = min(start + MAX_SOURCE_CHARACTERS, len(text))
        if end < len(text):
            cut = text.rfind("\n", start, end)
            if cut > start:
                end = cut + 1
        yield start, text[start:end]
        start = end


def _findings_in_pieces(
    text: str, enabled_entities: Iterable[str], custom: tuple
) -> tuple[tuple[Finding, ...], bool]:
    """Findings over the whole text, in source order, and whether a piece failed.

    The detector refuses more than `MAX_SOURCE_CHARACTERS` at once, and the
    caller used to pass the whole leaf and get nothing back: a 150 KB `Read`
    result reached the model with every secret in it, under a line saying
    inspection was incomplete.
    """
    entities = tuple(enabled_entities)
    found: list[Finding] = []
    partial = False
    for offset, piece in _pieces(text):
        try:
            findings = analyze(piece, entities, custom)
        except (ValueError, TimeoutError):
            # One bad piece must not cost the masking of every other piece.
            partial = True
            continue
        found.extend(
            Finding(
                entity_type=finding.entity_type,
                start=finding.start + offset,
                end=finding.end + offset,
                score=finding.score,
                label=finding.label,
            )
            for finding in findings
        )
    return tuple(found), partial


def evaluate(
    text: str,
    enabled_entities: Iterable[str] = ENTITY_TYPES,
    custom: tuple = (),
    reveal: dict | None = None,
) -> GuardDecision:
    partial = False
    if len(text) > MAX_SOURCE_CHARACTERS:
        findings, partial = _findings_in_pieces(text, enabled_entities, custom)
    else:
        findings = analyze(text, enabled_entities, custom)
    if not findings:
        return GuardDecision((), text, partial)

    counts: dict[str, int] = {}
    pieces: list[str] = []
    cursor = 0
    for finding in findings:
        counts[finding.entity_type] = counts.get(finding.entity_type, 0) + 1
        marker = f"{finding.entity_type}_{counts[finding.entity_type]}"
        digits = (reveal or {}).get(finding.entity_type, 0)
        tail = _tail(text[finding.start : finding.end], digits) if digits else ""
        pieces.append(text[cursor : finding.start])
        pieces.append(f"<{marker}:{tail}>" if tail else f"<{marker}>")
        cursor = finding.end
    pieces.append(text[cursor:])
    return GuardDecision(findings, "".join(pieces), partial)
