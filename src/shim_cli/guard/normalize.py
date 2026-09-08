from __future__ import annotations

import codecs
import re
import unicodedata
from dataclasses import dataclass

MAX_SOURCE_CHARACTERS = 100_000
MAX_NORMALIZED_CHARACTERS = 200_000

_INVISIBLE = re.compile(r"[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
_PERCENT_BYTE = re.compile(r"%([0-9A-Fa-f]{2})")
_SourceSpan = tuple[int, int]


@dataclass(frozen=True)
class NormalizedText:
    __slots__ = ("source_spans", "text")

    text: str
    source_spans: tuple[_SourceSpan, ...]


def _too_large() -> ValueError:
    return ValueError("Guard input exceeds the safe analysis limit.")


def _decode_percent(text: str) -> tuple[str, list[_SourceSpan]]:
    output: list[str] = []
    spans: list[_SourceSpan] = []
    index = 0
    while index < len(text):
        if not text[index].isascii():
            output.append(text[index])
            spans.append((index, index + 1))
            index += 1
            continue

        end = index + 1
        while end < len(text) and text[end].isascii():
            end += 1
        encoded: list[tuple[int, _SourceSpan]] = []
        while index < end:
            match = _PERCENT_BYTE.match(text, index)
            if match:
                encoded.append((int(match.group(1), 16), (index, index + 3)))
                index += 3
            else:
                encoded.append((ord(text[index]), (index, index + 1)))
                index += 1

        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        pending: list[_SourceSpan] = []
        for byte, span in encoded:
            pending.append(span)
            decoded = decoder.decode(bytes((byte,)))
            if not decoded:
                continue
            buffered = len(decoder.getstate()[0])
            consumed = pending[:-buffered] if buffered else pending
            if not consumed:
                raise ValueError("Guard normalization failed safely.")
            source_span = (consumed[0][0], consumed[-1][1])
            output.extend(decoded)
            spans.extend([source_span] * len(decoded))
            if len(output) > MAX_NORMALIZED_CHARACTERS:
                raise _too_large()
            pending = pending[-buffered:] if buffered else []
        decoded = decoder.decode(b"", final=True)
        if decoded:
            if not pending:
                raise ValueError("Guard normalization failed safely.")
            source_span = (pending[0][0], pending[-1][1])
            output.extend(decoded)
            spans.extend([source_span] * len(decoded))
        elif pending:
            raise ValueError("Guard normalization failed safely.")
        if len(output) > MAX_NORMALIZED_CHARACTERS:
            raise _too_large()
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
    for origin, (character, span) in enumerate(zip(text, spans, strict=True)):
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

    try:
        decoded, spans = _decode_percent(text)
    except UnicodeDecodeError as error:
        raise ValueError("Guard input contains malformed percent encoding.") from error
    visible_text: list[str] = []
    visible_spans: list[_SourceSpan] = []
    if len(decoded) != len(spans):
        raise ValueError("Guard normalization failed safely.")
    for character, span in zip(decoded, spans, strict=True):
        if not _INVISIBLE.fullmatch(character):
            visible_text.append(character)
            visible_spans.append(span)
    normalized, normalized_spans = _normalize_unicode(
        "".join(visible_text), visible_spans
    )
    return NormalizedText(normalized, normalized_spans)
