from __future__ import annotations

import itertools
import re
import sys
import time
from collections.abc import Iterable, Iterator
from typing import NamedTuple

# The parser `re` compiles with, to measure a pattern; 3.11 renamed it.
if sys.version_info >= (3, 11):
    from re import _parser as _regex_parser  # ty: ignore[unresolved-import]
else:
    import sre_parse as _regex_parser

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
    "TR_LICENSE_PLATE",
    "SECRET",
    "DB_URI",
)
# Coding traffic is full of plate-shaped strings (`[12 GET 200]`, `01 OCT 26`),
# so a plate is looked for only where the user's settings name the type.
OPT_IN_TYPES = ("TR_LICENSE_PLATE",)
# One more type is whatever the user defined; it has no built-in corpus.
CUSTOM = "CUSTOM"
ENTITY_TYPES = (*BUILT_IN_TYPES, CUSTOM)
DEFAULT_ENTITIES = tuple(name for name in ENTITY_TYPES if name not in OPT_IN_TYPES)

# The three types where a trailing digit group answers "which one" without
# giving the value back, as card issuers and banks already print them.
REVEALABLE = ("IBAN", "CREDIT_CARD", "PHONE")
MAX_REVEAL_DIGITS = 4
PLACEHOLDER = re.compile(rf"<[A-Z_]+_[0-9]+(?::[0-9]{{1,{MAX_REVEAL_DIGITS}}})?>")

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
            raise ValueError(
                "a custom pattern name must be UPPER_CASE letters, digits "
                "and underscores, up to 32 characters"
            )
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


# A member of each character class a pattern may name: digits, letters, space,
# line breaks and the usual separators.
_CLASS_MEMBERS = "1aA _\n\t.-:/,;@"
_PROBE_WORD = re.compile(r"[^\W_]{2,}")


# Past 1,024 characters the runs grow by a quarter at a time, so a count that
# only starts to backtrack past one length (`\d{1025}\d*\d*;`) is met just
# after it, while one search is still short.
_PROBE_LENGTHS = (
    *range(2, 65, 2),
    256,
    *(int(1_024 * 1.25**step) for step in range(11)),
    PROBE_LENGTH,
)
_EXTRAPOLATE_FROM = 1_024


def _reach(source: str, flags: int) -> int:
    """The most characters any part of the pattern needs, lookarounds included:
    no run shorter than this shows how the pattern grows past it."""
    widest = 0
    parts: list[object] = [_regex_parser.parse(source, flags)]
    while parts:
        part = parts.pop()
        if isinstance(part, _regex_parser.SubPattern):
            widest = max(widest, part.getwidth()[0])
            parts.extend(part.data)
        elif isinstance(part, (tuple, list)):
            parts.extend(part)
    return widest


def _ramp(unit: str, trailing: str) -> Iterator[tuple[int, str]]:
    for length in _PROBE_LENGTHS:
        yield length, (unit * (length // len(unit) + 1))[:length] + trailing


def _probes(source: str) -> Iterator[Iterator[tuple[int, str]]]:
    """Repeated input the pattern may meet: runs of each character it can name
    or writes, of two of them alternating, and of each word it writes. Each run
    grows from a few characters to `PROBE_LENGTH`, so an exponential pattern is
    stopped while it is still fast and a quadratic one is met at full length."""
    singles = sorted(set(_CLASS_MEMBERS + source))
    units = [
        *singles,
        *map("".join, itertools.combinations(singles, 2)),
        *sorted(set(_PROBE_WORD.findall(source))),
    ]
    trailing = next(
        (character for character in "!#~" if character not in source), "\x00"
    )
    for unit in units:
        yield _ramp(unit, trailing)


def unsafe_pattern(name: str, source: str, flags: int = 0) -> str:
    """The reason this pattern must not reach the hook, or an empty string.

    The hook has a deadline and `re` has no per-match timeout, so a pattern is
    checked when it is written rather than when it runs, compiled with the
    `flags` the hook gives it: `(?:id|ID)*` is linear until case is ignored.
    """
    if _BACKREFERENCE.search(source) or _NESTED_QUANTIFIER.search(source):
        return f"pattern {name} backtracks on repeated input; simplify it"
    try:
        regex = re.compile(source, flags)
    except re.error:
        return f"pattern {name} is not a valid regular expression"
    # A run stops early once it is far under the limit at twice the reach: time
    # that grows by at most the cube of the length past the reach cannot reach
    # the limit at full length, and a steeper one is already slow here.
    reach = _reach(source, flags)
    for ramp in _probes(source):
        for length, probe in ramp:
            started = time.perf_counter()
            regex.search(probe)
            elapsed = (time.perf_counter() - started) * 1000
            if elapsed > PROBE_MILLISECONDS:
                return f"pattern {name} backtracks on repeated input; simplify it"
            if (
                length >= max(_EXTRAPOLATE_FROM, 2 * reach)
                and elapsed * ((PROBE_LENGTH - reach) / (length - reach)) ** 3
                <= PROBE_MILLISECONDS
            ):
                break
    return ""
