from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_SOURCE_CHARACTERS = 100_000
MAX_NORMALIZED_CHARACTERS = 200_000
# NFC puts a run of combining marks in order by insertion sort, in one call
# the hook's deadline cannot interrupt: 100,000 marks took 6 s. A run is cut
# after 30 marks, as Unicode's stream-safe text format (UAX #15) does.
_MOST_MARKS = 30

_INVISIBLE = re.compile(r"[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
_PERCENT_RUN = re.compile(r"(?:%[0-9A-Fa-f]{2})+")
_SourceSpan = tuple[int, int]


@dataclass(frozen=True)
class NormalizedText:
    """`literal` is `text` with NUL for each character the source does not
    write as itself, as a `&` decoded from `%26` or folded from a fullwidth
    ampersand. A value ends only at a mark its source writes."""

    __slots__ = ("literal", "source_spans", "text")

    text: str
    source_spans: tuple[_SourceSpan, ...]
    literal: str


class InputTooLarge(ValueError):
    """`fits` is how many source characters from the start fit the limits:
    always at least one, and fewer than the text has."""

    def __init__(self, fits: int) -> None:
        super().__init__("Guard input exceeds the safe analysis limit.")
        self.fits = fits


def _decode_percent(text: str) -> tuple[str, list[_SourceSpan]]:
    output: list[str] = []
    spans: list[_SourceSpan] = []
    index = 0
    for run in _PERCENT_RUN.finditer(text):
        output.extend(text[index : run.start()])
        spans.extend((at, at + 1) for at in range(index, run.start()))
        escapes = range(run.start(), run.end(), 3)
        data = bytes(int(text[at + 1 : at + 3], 16) for at in escapes)
        consumed = 0
        for character in data.decode("utf-8", "surrogateescape"):
            start = escapes[consumed]
            if "\udc80" <= character <= "\udcff":
                output.extend(text[start : start + 3])
                spans.extend((at, at + 1) for at in range(start, start + 3))
                consumed += 1
                continue
            size = len(character.encode("utf-8"))
            output.append(character)
            spans.append((start, escapes[consumed + size - 1] + 3))
            consumed += size
        index = run.end()
    output.extend(text[index:])
    spans.extend((at, at + 1) for at in range(index, len(text)))
    return "".join(output), spans


def _normalization_continues(previous: str, current: str) -> bool:
    previous_code = ord(previous)
    current_code = ord(current)
    return bool(
        unicodedata.combining(current)
        or unicodedata.normalize("NFC", previous + current)
        != unicodedata.normalize("NFC", previous)
        + unicodedata.normalize("NFC", current)
        or (0x1161 <= previous_code <= 0x1175 and 0x11A8 <= current_code <= 0x11C2)
    )


def _normalize_unicode(
    text: str, spans: list[_SourceSpan]
) -> tuple[str, tuple[_SourceSpan, ...], str]:
    if len(text) != len(spans):
        raise ValueError("Guard normalization failed safely.")
    # One entry per source character, not per character it decomposes into:
    # U+FDFA writes eighteen.
    decomposed: list[str] = []
    # Where each run of characters that normalize together starts.
    cuts: list[int] = []
    size = marks = 0
    # No `strict=`: the zipapp runs on the 3.9 that ships with macOS, where
    # the keyword is a TypeError. The length check above is the same guard.
    for character, span in zip(text, spans):  # noqa: B905
        following = unicodedata.normalize("NFKD", character)
        size += len(following)
        if size > MAX_NORMALIZED_CHARACTERS:
            raise InputTooLarge(span[0])
        # A decomposition that starts with a mark is all marks.
        added = len(following) if unicodedata.combining(following[0]) else 0
        if (
            not decomposed
            or marks + added > _MOST_MARKS
            or not _normalization_continues(decomposed[-1][-1], following[0])
        ):
            cuts.append(len(decomposed))
            marks = 0
        marks = marks + added if added else 0
        decomposed.append(following)
    cuts.append(len(decomposed))

    output: list[str] = []
    literal: list[str] = []
    normalized_spans: list[_SourceSpan] = []
    for start, end in zip(cuts, cuts[1:]):  # noqa: B905
        normalized = unicodedata.normalize("NFC", "".join(decomposed[start:end]))
        source_span = (spans[start][0], spans[end - 1][1])
        output.append(normalized)
        # A one-character span is a source character written as itself. No
        # character's normalization holds it beside others, so a run is
        # literal whole or not at all.
        written = source_span[1] - source_span[0] == 1 and normalized == text[start]
        literal.append(normalized if written else "\0" * len(normalized))
        normalized_spans.extend([source_span] * len(normalized))
    return "".join(output), tuple(normalized_spans), "".join(literal)


def normalize(text: str) -> NormalizedText:
    if not isinstance(text, str):
        raise ValueError("Guard input must be text.")
    if len(text) > MAX_SOURCE_CHARACTERS:
        raise InputTooLarge(MAX_SOURCE_CHARACTERS)
    if text.isascii() and "%" not in text:
        return NormalizedText(
            text,
            tuple((index, index + 1) for index in range(len(text))),
            text,
        )

    decoded, spans = _decode_percent(text)
    visible_text: list[str] = []
    visible_spans: list[_SourceSpan] = []
    if len(decoded) != len(spans):
        raise ValueError("Guard normalization failed safely.")
    # No `strict=`; see _normalize_unicode. The check above is the guard.
    for character, span in zip(decoded, spans):  # noqa: B905
        if not _INVISIBLE.fullmatch(character):
            visible_text.append(character)
            visible_spans.append(span)
    return NormalizedText(*_normalize_unicode("".join(visible_text), visible_spans))
