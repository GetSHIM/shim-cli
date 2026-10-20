from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

from .analyze import _resolve_overlaps, analyze_counting
from .entities import DEFAULT_ENTITIES, ENTITY_TYPES, MAX_REVEAL_DIGITS
from .models import Finding, GuardDecision
from .normalize import MAX_SOURCE_CHARACTERS, InputTooLarge, normalize

_OVERLAP = 4_096
_ASCII_DIGITS = frozenset("0123456789")
# Types whose value is the same however it is spaced, punctuated or cased, so
# one account printed two ways keeps one number.
_LAYOUT_FREE = frozenset(
    (
        "IBAN",
        "CREDIT_CARD",
        "PHONE",
        "US_SSN",
        "TR_NATIONAL_ID",
        "TR_VKN",
        "MAC_ADDRESS",
        "TR_LICENSE_PLATE",
    )
)


# A placeholder of shim's shape, as text: its type and number, any tail after.
_NAMED = re.compile(
    rf"<({'|'.join(ENTITY_TYPES)})_([0-9]{{1,7}})(?::[0-9]{{1,{MAX_REVEAL_DIGITS}}})?>"
)
# The key in a numbering under which, per type, the highest number its event
# already holds as text is kept. No entity type is "".
FLOORS = ""
# A 1 MB event holds fewer than 200,000 values, so floors below this keep every
# number shim issues within the seven digits `_NAMED` reads.
_MAX_FLOOR = 1_000_000


def placeholder_names(text: str) -> Iterator[tuple[str, int]]:
    """Type and number of each placeholder of shim's shape in `text`."""
    for match in _NAMED.finditer(text):
        yield match.group(1), int(match.group(2))


def numbering_for(texts: Iterable[str]) -> dict:
    """A numbering for one event that issues no placeholder its `texts`
    already hold: each type's numbers start above the highest one written.
    """
    floors: dict = {}
    for text in texts:
        for kind, number in placeholder_names(text):
            if number < _MAX_FLOOR:
                floors[kind] = max(floors.get(kind, 0), number)
    return {FLOORS: floors}


def issued(numbering: dict) -> tuple:
    """(type, first, last) for each type's numbers in `numbering`: the names
    it gave, never the values it gave them to."""
    floors = numbering.get(FLOORS, {})
    return tuple(
        (kind, floors.get(kind, 0) + 1, floors.get(kind, 0) + len(given))
        for kind, given in sorted(numbering.items())
        if kind != FLOORS and given
    )


def line_breaks(span: str) -> str:
    """The line breaks a placeholder keeps after it, so every line after a
    masked key or wrapped block keeps its number."""
    return "".join(character for character in span if character in "\r\n")


def _value(span: str) -> str:
    """The value as the detector read it: decoded, and in NFKC form."""
    try:
        return normalize(span).text
    except InputTooLarge:  # longer than one analysis reads: as written
        return span


def _key(entity_type: str, value: str) -> str:
    if entity_type in _LAYOUT_FREE:
        return "".join(character for character in value if character.isalnum()).upper()
    return value


def _tail(value: str, digits: int) -> str:
    """The last few digits of a decoded value, in ASCII, so a `%20` adds none."""
    found = [character for character in value if character in _ASCII_DIGITS]
    return "".join(found[-digits:])


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
    text: str,
    enabled_entities: Iterable[str],
    custom: tuple,
    size: int = MAX_SOURCE_CHARACTERS,
) -> tuple[tuple[Finding, ...], bool, int]:
    """Findings over the whole text, in source order, and whether a piece failed.

    The detector refuses more than `MAX_SOURCE_CHARACTERS` at once, and the
    caller used to pass the whole leaf and get nothing back: a 150 KB `Read`
    result reached the model with every secret in it, under a line saying
    inspection was incomplete. `size` is the first cut: the limit, or as much
    as fitted when the whole text was refused.
    """
    entities = tuple(enabled_entities)
    found: list[Finding] = []
    partial = False
    bare_numbers = 0
    pending = list(_pieces(text, size))[::-1]
    while pending:
        offset, piece = pending.pop()
        try:
            findings, bare = analyze_counting(piece, entities, custom)
        except InputTooLarge as error:
            # It grew past the limit when normalised, as U+FDFA does eighteen
            # times over: cut it where it stopped fitting, so no work repeats.
            parts = _pieces(piece, error.fits)
            pending.extend(
                (offset + start, part) for start, part in reversed(list(parts))
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
                value=None
                if finding.value is None
                else (finding.value[0] + offset, finding.value[1] + offset),
            )
            for finding in findings
        )
    return tuple(_resolve_overlaps(found)), partial, bare_numbers


def evaluate(
    text: str,
    enabled_entities: Iterable[str] = DEFAULT_ENTITIES,
    custom: tuple = (),
    reveal: dict | None = None,
    numbering: dict | None = None,
) -> GuardDecision:
    """`text` with each value found replaced by a numbered placeholder.

    A value keeps one number and two values never share one. `numbering`
    holds the numbers given so far, by type and then value: pass one dict to
    every call for one event, and the numbers run across all its fields. It
    is never written anywhere. Without one, numbers start above any
    placeholder `text` already holds.
    """
    entities = tuple(enabled_entities)
    partial = False
    if len(text) > MAX_SOURCE_CHARACTERS:
        findings, partial, bare_numbers = _findings_in_pieces(text, entities, custom)
    else:
        try:
            findings, bare_numbers = analyze_counting(text, entities, custom)
        except InputTooLarge as error:
            findings, partial, bare_numbers = _findings_in_pieces(
                text, entities, custom, error.fits
            )
    if not findings:
        return GuardDecision((), text, partial, bare_numbers)

    numbers = numbering_for((text,)) if numbering is None else numbering
    floors = numbers.get(FLOORS, {})
    pieces: list[str] = []
    cursor = 0
    for finding in findings:
        span = text[finding.start : finding.end]
        value = _value(span)
        given = numbers.setdefault(finding.entity_type, {})
        number = given.setdefault(
            _key(finding.entity_type, value),
            floors.get(finding.entity_type, 0) + len(given) + 1,
        )
        marker = f"{finding.entity_type}_{number}"
        digits = (reveal or {}).get(finding.entity_type, 0)
        tail = ""
        if digits:
            # Read from the value alone, never from a neighbour masked with it.
            own = finding.value
            tail = _tail(
                value if own is None else _value(text[own[0] : own[1]]), digits
            )
        pieces.append(text[cursor : finding.start])
        pieces.append(f"<{marker}:{tail}>" if tail else f"<{marker}>")
        pieces.append(line_breaks(span))
        cursor = finding.end
    pieces.append(text[cursor:])
    return GuardDecision(findings, "".join(pieces), partial, bare_numbers)
