from __future__ import annotations

import re
import time
from collections.abc import Iterable
from typing import NamedTuple

BUILT_IN_TYPES = (
    "EMAIL",
    "PHONE",
    "CREDIT_CARD",
    "IBAN",
    "IP_ADDRESS",
    "MAC_ADDRESS",
    "US_SSN",
    "TR_NATIONAL_ID",
    "TR_VKN",
    "SECRET",
    "DB_URI",
)
# The twelfth type is whatever the user defined; it has no built-in corpus.
CUSTOM = "CUSTOM"
ENTITY_TYPES = (*BUILT_IN_TYPES, CUSTOM)
DEFAULT_ENTITIES = ENTITY_TYPES

# The three types where a trailing digit group answers "which one" without
# giving the value back, as card issuers and banks already print them.
REVEALABLE = ("IBAN", "CREDIT_CARD", "PHONE")
MAX_REVEAL_DIGITS = 4

MAX_CUSTOM_PATTERNS = 32
MAX_PATTERN_CHARS = 256
MIN_LITERAL_CHARS = 3
DEFAULT_SCORE = 0.5
PROBE_MILLISECONDS = 100
PROBE_LENGTH = 10_000
_CUSTOM_KEYS = {"name", "pattern", "literal", "score", "ignore_case", "whole_word"}
_CUSTOM_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_BACKREFERENCE = re.compile(r"\\[1-9]")
# A quantified group that already contains a quantifier: the classic blow-up.
_NESTED_QUANTIFIER = re.compile(r"\([^()]*[*+{][^()]*\)\s*[*+{]")


class CustomPattern(NamedTuple):
    name: str
    regex: re.Pattern[str]
    score: float
    # The entry as written, so `shim config` can rewrite the file it came from.
    entry: dict


def normalize_entities(entities: Iterable[object]) -> tuple[str, ...]:
    values = tuple(entities)
    if any(not isinstance(value, str) for value in values):
        raise ValueError("entity names must be strings")
    selected = set(values)
    if len(values) != len(selected):
        raise ValueError("entity names must not be repeated")
    unknown = selected.difference(ENTITY_TYPES)
    if unknown:
        raise ValueError("unsupported entity name")
    return tuple(entity for entity in ENTITY_TYPES if entity in selected)


def normalize_reveal(section: object) -> dict:
    if not isinstance(section, dict):
        raise ValueError("a reveal table is invalid")
    reveal: dict = {}
    for key, value in section.items():
        if key not in REVEALABLE:
            raise ValueError("that entity cannot reveal a tail")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("a reveal length is invalid")
        if not 1 <= value <= MAX_REVEAL_DIGITS:
            raise ValueError("a reveal length is invalid")
        reveal[key] = value
    return dict(sorted(reveal.items()))


def _literal_source(text: str, whole_word: bool) -> str:
    escaped = re.escape(text)
    # \b fails beside punctuation; a lookaround holds for any literal.
    return rf"(?<!\w){escaped}(?!\w)" if whole_word else escaped


def entry_source(entry: dict) -> str:
    pattern = entry.get("pattern")
    literal = entry.get("literal")
    if (pattern is None) == (literal is None):
        raise ValueError("a custom entry needs exactly one of pattern or literal")
    if pattern is not None:
        if "whole_word" in entry:
            raise ValueError("whole_word applies to a literal, not a pattern")
        if not isinstance(pattern, str) or not 0 < len(pattern) <= MAX_PATTERN_CHARS:
            raise ValueError("a custom pattern is invalid")
        return pattern
    if not isinstance(literal, str) or len(literal) < MIN_LITERAL_CHARS:
        raise ValueError("a custom literal is invalid")
    whole_word = entry.get("whole_word", True)
    if not isinstance(whole_word, bool):
        raise ValueError("whole_word must be true or false")
    return _literal_source(literal, whole_word)


def compile_custom(entries: Iterable[object]) -> tuple[CustomPattern, ...]:
    """Validate and compile the user's patterns. Timing checks belong to the CLI."""
    items = list(entries)
    if len(items) > MAX_CUSTOM_PATTERNS:
        raise ValueError("too many custom patterns")
    compiled: list[CustomPattern] = []
    seen: set[str] = set()
    for entry in items:
        if not isinstance(entry, dict) or not set(entry) <= _CUSTOM_KEYS:
            raise ValueError("a custom entry is invalid")
        name = entry.get("name")
        if not isinstance(name, str) or not _CUSTOM_NAME.match(name):
            raise ValueError("a custom pattern name is invalid")
        if name in seen:
            raise ValueError("custom pattern names must not be repeated")
        seen.add(name)
        score = entry.get("score", DEFAULT_SCORE)
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("a custom score is invalid")
        if not 0 <= score <= 1:
            raise ValueError("a custom score is invalid")
        ignore_case = entry.get("ignore_case", False)
        if not isinstance(ignore_case, bool):
            raise ValueError("ignore_case must be true or false")
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(entry_source(entry), flags)
        except re.error as error:
            raise ValueError("a custom pattern is invalid") from error
        compiled.append(CustomPattern(name, regex, float(score), dict(entry)))
    return tuple(compiled)


def _probes(source: str) -> tuple[str, ...]:
    letters = [character for character in source if character.isalnum()]
    common = max(set(letters), key=letters.count) if letters else "a"
    trailing = next(
        (character for character in "!#~" if character not in source), "\x00"
    )
    return (common * PROBE_LENGTH + trailing, "\n" * PROBE_LENGTH)


def unsafe_pattern(name: str, source: str) -> str:
    """The reason this pattern must not reach the hook, or an empty string.

    The hook has a deadline and `re` has no per-match timeout, so a pattern is
    checked when it is written rather than when it runs.
    """
    if _BACKREFERENCE.search(source) or _NESTED_QUANTIFIER.search(source):
        return f"pattern {name} backtracks on repeated input; simplify it"
    try:
        regex = re.compile(source)
    except re.error:
        return f"pattern {name} is not a valid regular expression"
    for probe in _probes(source):
        started = time.perf_counter()
        regex.search(probe)
        if (time.perf_counter() - started) * 1000 > PROBE_MILLISECONDS:
            return f"pattern {name} backtracks on repeated input; simplify it"
    return ""
