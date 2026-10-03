from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_SOURCE_CHARACTERS = 100_000
MAX_NORMALIZED_CHARACTERS = 200_000

_INVISIBLE = re.compile(r"[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
_PERCENT_RUN = re.compile(r"(?:%[0-9A-Fa-f]{2})+")
_SourceSpan = tuple[int, int]


@dataclass(frozen=True)
class NormalizedText:
    __slots__ = ("source_spans", "text")

    text: str
    source_spans: tuple[_SourceSpan, ...]


class InputTooLarge(ValueError):
    pass


def _too_large() -> InputTooLarge:
    return InputTooLarge("Guard input exceeds the safe analysis limit.")


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
) -> tuple[str, tuple[_SourceSpan, ...]]:
    characters: list[str] = []
    decomposed_spans: list[_SourceSpan] = []
    origins: list[int] = []
    if len(text) != len(spans):
        raise ValueError("Guard normalization failed safely.")
    # No `strict=`: the zipapp runs on the 3.9 that ships with macOS, where
    # the keyword is a TypeError. The length check above is the same guard.
    for origin, (character, span) in enumerate(zip(text, spans)):  # noqa: B905
        decomposed = unicodedata.normalize("NFKD", character)
        if len(characters) + len(decomposed) > MAX_NORMALIZED_CHARACTERS:
            raise _too_large()
        characters.extend(decomposed)
        decomposed_spans.extend([span] * len(decomposed))
        origins.extend([origin] * len(decomposed))

    output: list[str] = []
    normalized_spans: list[_SourceSpan] = []
    start = 0
    for index in range(1, len(characters) + 1):
        if index < len(characters) and (
            origins[index] == origins[index - 1]
            or _normalization_continues(characters[index - 1], characters[index])
        ):
            continue
        normalized = unicodedata.normalize("NFC", "".join(characters[start:index]))
        affected = decomposed_spans[start:index]
        if not affected:
            raise ValueError("Guard normalization failed safely.")
        if len(output) + len(normalized) > MAX_NORMALIZED_CHARACTERS:
            raise _too_large()
        source_span = (affected[0][0], affected[-1][1])
        output.extend(normalized)
        normalized_spans.extend([source_span] * len(normalized))
        start = index
    return "".join(output), tuple(normalized_spans)


def normalize(text: str) -> NormalizedText:
    if not isinstance(text, str):
        raise ValueError("Guard input must be text.")
    if len(text) > MAX_SOURCE_CHARACTERS:
        raise _too_large()
    if text.isascii() and "%" not in text:
        return NormalizedText(
            text,
            tuple((index, index + 1) for index in range(len(text))),
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
    normalized, normalized_spans = _normalize_unicode(
        "".join(visible_text), visible_spans
    )
    return NormalizedText(normalized, normalized_spans)
