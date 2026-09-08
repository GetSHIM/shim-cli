from __future__ import annotations

JSON_COMPACTION = "json"
TRAILING_WHITESPACE = "whitespace"
TRANSFORMS = (JSON_COMPACTION, TRAILING_WHITESPACE)
DEFAULT_TRANSFORMS = (JSON_COMPACTION,)

MIN_CANDIDATE_CHARS = 64

_WHITESPACE = " \t\r\n"
_STRUCTURAL = "{}[]:,"


def compact_json(text: str) -> str:
    if not text or text[0] not in "{[":
        return text
    out = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == '"':
            end = _string_end(text, index)
            if end is None:
                return text
            out.append(text[index:end])
            index = end
            continue
        if character in _WHITESPACE:
            index += 1
            continue
        if character not in _STRUCTURAL and not _is_literal_start(character):
            return text
        out.append(character)
        index += 1
    compacted = "".join(out)
    if not _same_value(text, compacted):
        return text
    return compacted


def _is_literal_start(character: str) -> bool:
    return character.isalnum() or character in "+-."


def _string_end(text: str, start: int) -> int | None:
    index = start + 1
    length = len(text)
    while index < length:
        character = text[index]
        if character == "\\":
            index += 2
            continue
        if character == '"':
            return index + 1
        index += 1
    return None


def _same_value(original: str, compacted: str) -> bool:
    import json

    try:
        return json.loads(original) == json.loads(compacted)
    except (ValueError, RecursionError):
        return False


def strip_trailing_whitespace(text: str) -> str:
    if not any(character in text for character in " \t"):
        return text
    lines = text.split("\n")
    stripped = [line.rstrip(" \t") for line in lines]
    if stripped == lines:
        return text
    return "\n".join(stripped)


_APPLY = {
    JSON_COMPACTION: compact_json,
    TRAILING_WHITESPACE: strip_trailing_whitespace,
}


def shrink(text: str, enabled: tuple = DEFAULT_TRANSFORMS) -> tuple:
    if len(text) < MIN_CANDIDATE_CHARS:
        return text, ()
    applied = []
    current = text
    for name in TRANSFORMS:
        if name not in enabled:
            continue
        candidate = _APPLY[name](current)
        if len(candidate) < len(current):
            current = candidate
            applied.append(name)
    return current, tuple(applied)


__all__ = [
    "DEFAULT_TRANSFORMS",
    "JSON_COMPACTION",
    "MIN_CANDIDATE_CHARS",
    "TRAILING_WHITESPACE",
    "TRANSFORMS",
    "compact_json",
    "shrink",
    "strip_trailing_whitespace",
]
