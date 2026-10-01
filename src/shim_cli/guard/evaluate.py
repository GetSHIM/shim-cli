from __future__ import annotations

from collections.abc import Iterable, Iterator

from .analyze import _resolve_overlaps, analyze_counting
from .entities import ENTITY_TYPES
from .models import Finding, GuardDecision
from .normalize import MAX_SOURCE_CHARACTERS, InputTooLarge

_SMALLEST_PIECE = MAX_SOURCE_CHARACTERS // 4
_OVERLAP = 4_096


def _tail(span: str, digits: int) -> str:
    """The last few digits, ignoring the separators the value was printed with."""
    return "".join(character for character in span if character.isdigit())[-digits:]


def _pieces(text: str, size: int = MAX_SOURCE_CHARACTERS) -> Iterator[tuple[int, str]]:
    """Cut on the last newline before each limit, so no line is split.

    A line longer than the limit is cut where it must be, and the next piece
    starts `_OVERLAP` characters earlier, so a value of that length or less on
    the cut is read whole by one of the two; overlapping findings are united. A
    value that spans a line break is the residual risk, recorded in
    `docs/privacy.md`.
    """
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        cut = text.rfind("\n", start, end) if end < len(text) else -1
        if cut > start:
            end = cut + 1
        yield start, text[start:end]
        if end == len(text):
            return
        start = end if cut > start else max(end - _OVERLAP, start + 1)


def _findings_in_pieces(
    text: str, enabled_entities: Iterable[str], custom: tuple
) -> tuple[tuple[Finding, ...], bool, int]:
    """Findings over the whole text, in source order, and whether a piece failed.

    The detector refuses more than `MAX_SOURCE_CHARACTERS` at once, and the
    caller used to pass the whole leaf and get nothing back: a 150 KB `Read`
    result reached the model with every secret in it, under a line saying
    inspection was incomplete.
    """
    entities = tuple(enabled_entities)
    found: list[Finding] = []
    partial = False
    bare_numbers = 0
    pending = list(_pieces(text))[::-1]
    while pending:
        offset, piece = pending.pop()
        try:
            findings, bare = analyze_counting(piece, entities, custom)
        except InputTooLarge:
            if len(piece) <= _SMALLEST_PIECE:
                partial = True
                continue
            halves = _pieces(piece, (len(piece) + 1) // 2)
            pending.extend(
                (offset + start, part) for start, part in reversed(list(halves))
            )
            continue
        except ValueError as error:
            partial = True
            if isinstance(error.__cause__, TimeoutError):
                break
            # One bad piece must not cost the masking of every other piece.
            continue
        bare_numbers += bare
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
    return tuple(_resolve_overlaps(found)), partial, bare_numbers


def evaluate(
    text: str,
    enabled_entities: Iterable[str] = ENTITY_TYPES,
    custom: tuple = (),
    reveal: dict | None = None,
) -> GuardDecision:
    entities = tuple(enabled_entities)
    partial = False
    if len(text) > MAX_SOURCE_CHARACTERS:
        findings, partial, bare_numbers = _findings_in_pieces(text, entities, custom)
    else:
        try:
            findings, bare_numbers = analyze_counting(text, entities, custom)
        except InputTooLarge:
            if len(text) <= _SMALLEST_PIECE:
                raise
            findings, partial, bare_numbers = _findings_in_pieces(
                text, entities, custom
            )
    if not findings:
        return GuardDecision((), text, partial, bare_numbers)

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
    return GuardDecision(findings, "".join(pieces), partial, bare_numbers)
