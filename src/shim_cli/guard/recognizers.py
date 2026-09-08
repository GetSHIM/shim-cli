"""Presidio 2.2.364 patterns (MIT, Microsoft)."""

from __future__ import annotations

import ipaddress
import re
import string
from typing import TYPE_CHECKING, NamedTuple

from .iban_patterns import regex_per_country
from .suffixes import is_registrable

if TYPE_CHECKING:
    from collections.abc import Callable

ENTITY_MAP = {
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "CREDIT_CARD": "CREDIT_CARD",
    "IBAN_CODE": "IBAN",
    "IP_ADDRESS": "IP_ADDRESS",
    "MAC_ADDRESS": "MAC_ADDRESS",
    "US_SSN": "US_SSN",
    "TR_NATIONAL_ID": "TR_NATIONAL_ID",
    "TR_VKN": "TR_VKN",
    "SECRET": "SECRET",
    "DB_URI": "DB_URI",
}

_FLAGS = re.DOTALL | re.MULTILINE | re.IGNORECASE
_IBAN_FLAGS = re.DOTALL | re.MULTILINE
_TRAILING_PROSE = ".,;:!?)]}>"
_PHONE_REGIONS = ("US", "GB", "DE", "FR", "IL", "IN", "CA", "BR", "TR")
_PHONE_SCORE = 0.4
_PHONE_LENIENCY = 1


class Match(NamedTuple):
    entity_type: str
    start: int
    end: int
    score: float


def _compile(
    patterns: tuple[tuple[str, float], ...], flags: int = _FLAGS
) -> tuple[tuple[re.Pattern[str], float], ...]:
    return tuple((re.compile(source, flags), score) for source, score in patterns)


def deduplicate(results: list[Match]) -> list[Match]:
    ordered = sorted(
        set(results),
        key=lambda item: (
            -item.score,
            item.start,
            -(item.end - item.start),
            item.entity_type,
        ),
    )
    starts: dict[str, set[int]] = {}
    for item in ordered:
        starts.setdefault(item.entity_type, set()).add(item.start)
    indices = {
        entity: {start: i for i, start in enumerate(sorted(offsets), 1)}
        for entity, offsets in starts.items()
    }
    # Prefix-maximum Fenwick trees preserve score order in O(n log n).
    trees = {entity: [-1] * (len(offsets) + 1) for entity, offsets in starts.items()}
    kept: list[Match] = []
    for item in ordered:
        tree = trees[item.entity_type]
        index = indices[item.entity_type][item.start]
        cursor, furthest = index, -1
        while cursor:
            furthest = max(furthest, tree[cursor])
            cursor -= cursor & -cursor
        if furthest >= item.end:
            continue
        kept.append(item)
        while index < len(tree):
            tree[index] = max(tree[index], item.end)
            index += index & -index
    return kept


def _scan(
    text: str,
    entity: str,
    patterns: tuple[tuple[re.Pattern[str], float], ...],
    validate: Callable[[str], bool | None] | None = None,
    invalidate: Callable[[str], bool] | None = None,
) -> list[Match]:
    results: list[Match] = []
    for regex, score in patterns:
        for match in regex.finditer(text):
            start, end = match.span()
            current = match.group()
            if not current:
                continue
            value = score
            if validate is not None:
                verdict = validate(current)
                if verdict is not None:
                    value = 1.0 if verdict else 0.0
            if invalidate is not None and invalidate(current):
                value = 0.0
            if value > 0:
                results.append(Match(entity, start, end, value))
    return deduplicate(results)


def _trim_trailing_prose(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1] in _TRAILING_PROSE:
        end -= 1
    return end


_EMAIL_PATTERNS = _compile(
    (
        (
            r"\b((([!#$%&'*+\-/=?^_`{|}~\w])|([!#$%&'*+\-/=?^_`{|}~\w]"
            r"[!#$%&'*+\-/=?^_`{|}~\.\w]{0,}[!#$%&'*+\-/=?^_`{|}~\w]))"
            r"[@]\w+(?:-+\w+)*(?:\.\w+(?:-+\w+)*)+)\b",
            0.5,
        ),
    )
)


def _validate_email(text: str) -> bool:
    return is_registrable(text.rpartition("@")[-1])


_CARD_PATTERNS = _compile(
    (
        (
            r"\b(?!1\d{12}(?!\d))((4\d{3})|(5[0-5]\d{2})|(6\d{3})|(1\d{3})"
            r"|(3\d{3}))[- ]?(\d{3,4})[- ]?(\d{3,4})[- ]?(\d{3,5})\b",
            0.3,
        ),
    )
)


def _validate_card(text: str) -> bool:
    digits = [int(character) for character in text.replace("-", "").replace(" ", "")]
    checksum = sum(digits[-1::-2])
    for digit in digits[-2::-2]:
        checksum += sum(int(character) for character in str(digit * 2))
    return checksum % 10 == 0


_IP_PATTERNS = _compile(
    (
        (
            r"(?<![\w:])::(?:ffff(?::0{1,4})?:)?"
            r"(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
            r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)"
            r"(?:/(?:12[0-8]|1[01]\d|[1-9]?\d))?\b",
            0.6,
        ),
        (
            r"(?<![\w:])(?:(?:[0-9A-Fa-f]{1,4}:){1,5}:(?:[0-9A-Fa-f]{1,4}:){0,4}"
            r"|(?:[0-9A-Fa-f]{1,4}:){6})"
            r"(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
            r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)"
            r"(?:/(?:12[0-8]|1[01]\d|[1-9]?\d))?\b",
            0.6,
        ),
        (
            r"\b(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\."
            r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\."
            r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\."
            r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)"
            r"(?:/(?:[0-2]?\d|3[0-2]))?\b",
            0.6,
        ),
        (
            r"(?<![\w:])(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
            r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:"
            r"|:(?::[0-9A-Fa-f]{1,4}){1,7}"
            r"|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}"
            r"|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}"
            r"|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}"
            r"|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}"
            r"|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}"
            r"|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}"
            r"|:(?::[0-9A-Fa-f]{1,4}){1,6})"
            r"(?:%[0-9a-zA-Z]+)?(?:/(?:12[0-8]|1[01]\d|[1-9]?\d))?(?![\w:]|\.\d)",
            0.6,
        ),
    )
)


def _invalidate_ip(text: str) -> bool:
    try:
        parsed = ipaddress.ip_interface(text)
    except ValueError:
        return True
    address = parsed.ip
    return address.is_loopback or address.is_unspecified


_MAC_PATTERNS = _compile(
    (
        (r"\b[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}\b", 0.6),
        (r"\b[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\b", 0.6),
    )
)
_MAC_SEPARATORS = re.compile(r"[:\-.]")
_MAC_HEX = re.compile(r"[0-9A-Fa-f]{12}")


def _invalidate_mac(text: str) -> bool:
    cleaned = _MAC_SEPARATORS.sub("", text)
    if _MAC_HEX.fullmatch(cleaned) is None:
        return True
    return cleaned.upper() in ("FFFFFFFFFFFF", "000000000000")


_SSN_PATTERNS = _compile(((r"\b([0-9]{3})[- .]([0-9]{2})[- .]([0-9]{4})\b", 0.5),))
_SSN_DENY = ("123456789", "987654320", "078051120")


def _invalidate_ssn(text: str) -> bool:
    delimiters = {character for character in text if character in (".", "-", " ")}
    if len(delimiters) > 1:
        return True
    digits = "".join(character for character in text if character.isdigit())
    if all(digits[0] == character for character in digits):
        return True
    if digits[3:5] == "00" or digits[5:] == "0000":
        return True
    if digits[:3] in ("000", "666"):
        return True
    return digits in _SSN_DENY


_TCKN_PATTERNS = _compile(((r"\b[1-9][0-9]{10}\b", 0.3),))


def _validate_tckn(text: str) -> bool:
    if len(text) != 11 or not text.isdigit() or text[0] == "0":
        return False
    digits = [int(character) for character in text]
    odd = sum(digits[index] for index in range(0, 9, 2))
    even = sum(digits[index] for index in range(1, 8, 2))
    if (odd * 7 - even) % 10 != digits[9]:
        return False
    return sum(digits[:10]) % 10 == digits[10]


_VKN_PATTERNS = _compile(((r"(?<!\d)\d{10}(?!\d)", 0.4),))
_VKN_CONTEXT = re.compile(
    r"(?i)\b(?:vkn|vergi(?:\s+(?:kimlik|numarası))?|tax(?:\s+id)?)\b"
)
_VKN_WINDOW = 32


def _validate_vkn(text: str) -> bool:
    digits = [int(character) for character in text]
    if len(digits) != 10 or len(set(digits)) == 1:
        return False
    checksum = 0
    for index, digit in enumerate(digits[:9]):
        adjusted = (digit + 9 - index) % 10
        if adjusted:
            checksum += (adjusted * 2 ** (9 - index)) % 9
    return digits[-1] == (10 - checksum % 10) % 10


def _scan_vkn(text: str) -> list[Match]:
    results = _scan(text, "TR_VKN", _VKN_PATTERNS, validate=_validate_vkn)
    return [
        result
        for result in results
        if _VKN_CONTEXT.search(
            text[
                max(0, result.start - _VKN_WINDOW) : min(
                    len(text), result.end + _VKN_WINDOW
                )
            ]
        )
    ]


_IBAN_PATTERN = re.compile(
    r"(?<![A-Z0-9])([A-Z]{2}[0-9]{2}(?:[ -]?[A-Z0-9]{4}){2,6})"
    r"((?:[ -]?[A-Z0-9]{4})?)((?:[ -]?[A-Z0-9]{1,3})?)(?![A-Z0-9])",
    _IBAN_FLAGS,
)
_IBAN_SCORE = 0.5
_IBAN_LETTERS: dict[int, str] = {
    ord(character): str(index)
    for index, character in enumerate(string.digits + string.ascii_uppercase)
}
_IBAN_COUNTRY = {
    country: re.compile(pattern, _IBAN_FLAGS)
    for country, pattern in regex_per_country.items()
}


def _iban_check_digits(iban: str) -> str:
    transformed = (iban[:2] + "00" + iban[4:]).upper()
    numeric = (transformed[4:] + transformed[:4]).translate(_IBAN_LETTERS)
    return f"{98 - (int(numeric) % 97):0>2}"


def _iban_format_matches(iban: str) -> bool:
    country = _IBAN_COUNTRY.get(iban[:2])
    return country is not None and country.match(iban) is not None


def _validate_iban(text: str) -> bool | None:
    try:
        value = text.replace("-", "").replace(" ", "")
        if _iban_check_digits(value) != value[2:4]:
            return False
        if _iban_format_matches(value):
            return True
        if _iban_format_matches(value.upper()):
            return None
        return False
    except ValueError:
        return False


def _scan_iban(text: str) -> list[Match]:
    results: list[Match] = []
    for match in _IBAN_PATTERN.finditer(text):
        for group in reversed(range(1, len(match.groups()) + 1)):
            start = match.span(0)[0]
            end = match.span(group)[1] if match.span(group)[1] > 0 else match.span(0)[1]
            current = text[start:end]
            if not current:
                continue
            score = _IBAN_SCORE
            verdict = _validate_iban(current)
            if verdict is not None:
                score = 1.0 if verdict else 0.0
            if score > 0:
                results.append(Match("IBAN_CODE", start, end, score))
                break
    return results


def _scan_phone(text: str) -> list[Match]:
    import phonenumbers

    results = [
        Match("PHONE_NUMBER", match.start, match.end, _PHONE_SCORE)
        for region in _PHONE_REGIONS
        for match in phonenumbers.PhoneNumberMatcher(
            text, region, leniency=_PHONE_LENIENCY
        )
    ]
    return deduplicate(results)


_SECRET_KEY = (
    r"password|passwd|pwd|api[_-]?key|secret|token|db[_-]?pass|postgres_password"
)
_SECRET_ASSIGNMENT = r"(?<![\w-])[\"']?(?:" + _SECRET_KEY + r")[\"']?\s*(?:=|:)\s*"
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str | None, float, bool], ...] = (
    (
        re.compile(
            r"-----BEGIN "
            r"(?P<key_type>(?:(?:RSA|EC|OPENSSH|ENCRYPTED) )?PRIVATE KEY)"
            r"-----[\s\S]*?(?:-----END (?P=key_type)-----|\Z)"
        ),
        None,
        0.99,
        False,
    ),
    (
        re.compile(
            r"(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|"
            r"sk_(?:live|test)_[A-Za-z0-9]{16,}|"
            r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
            r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|"
            r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
        ),
        None,
        0.99,
        False,
    ),
    (
        re.compile(
            r"https://(?:hooks\.slack\.com/services|"
            r"discord(?:app)?\.com/api/webhooks)/[^\s'\"]+",
            re.IGNORECASE,
        ),
        None,
        0.99,
        True,
    ),
    (
        re.compile(
            _SECRET_ASSIGNMENT + r"(?P<quote>[\"'])(?P<value>[^\r\n]{6,}?)(?P=quote)",
            re.IGNORECASE,
        ),
        "value",
        0.97,
        False,
    ),
    (
        re.compile(
            _SECRET_ASSIGNMENT + r"(?P<value>[^\s,}\]\"']{6,})",
            re.IGNORECASE,
        ),
        "value",
        0.97,
        False,
    ),
    (
        re.compile(
            r"--password(?:=|\s+)(?P<quote>[\"'])(?P<value>[^\r\n]{6,}?)(?P=quote)",
            re.IGNORECASE,
        ),
        "value",
        0.97,
        False,
    ),
    (
        re.compile(r"--password(?:=|\s+)(?P<value>[^\s]+)", re.IGNORECASE),
        "value",
        0.97,
        False,
    ),
)


def _scan_secret(text: str) -> list[Match]:
    results: list[Match] = []
    for pattern, value_group, score, trim in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(value_group) if value_group else match.span()
            if trim:
                end = _trim_trailing_prose(text, start, end)
            if start < end:
                results.append(Match("SECRET", start, end, score))
    return deduplicate(results)


_DB_URI_PATTERN = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis(?:s)?|"
    r"mssql)://[^\s'\"<>]+"
)
_LOOPBACK = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def _is_local_and_open(uri: str) -> bool:
    authority = uri.split("://", 1)[1].split("/", 1)[0]
    if "@" in authority:
        return False
    if authority.startswith("["):
        host = authority[1:].split("]", 1)[0]
    elif authority.count(":") == 1:
        host = authority.rsplit(":", 1)[0]
    else:
        host = authority
    return host.lower() in _LOOPBACK


def _scan_db_uri(text: str) -> list[Match]:
    results: list[Match] = []
    for match in _DB_URI_PATTERN.finditer(text):
        start, end = match.span()
        end = _trim_trailing_prose(text, start, end)
        if start >= end:
            continue
        if _is_local_and_open(text[start:end]):
            continue
        results.append(Match("DB_URI", start, end, 0.99))
    return results


_RECOGNIZERS: tuple[tuple[str, Callable[[str], list[Match]]], ...] = (
    (
        "EMAIL_ADDRESS",
        lambda text: _scan(
            text, "EMAIL_ADDRESS", _EMAIL_PATTERNS, validate=_validate_email
        ),
    ),
    ("PHONE_NUMBER", _scan_phone),
    (
        "CREDIT_CARD",
        lambda text: _scan(
            text, "CREDIT_CARD", _CARD_PATTERNS, validate=_validate_card
        ),
    ),
    ("IBAN_CODE", _scan_iban),
    (
        "IP_ADDRESS",
        lambda text: _scan(text, "IP_ADDRESS", _IP_PATTERNS, invalidate=_invalidate_ip),
    ),
    (
        "MAC_ADDRESS",
        lambda text: _scan(
            text, "MAC_ADDRESS", _MAC_PATTERNS, invalidate=_invalidate_mac
        ),
    ),
    (
        "US_SSN",
        lambda text: _scan(text, "US_SSN", _SSN_PATTERNS, invalidate=_invalidate_ssn),
    ),
    (
        "TR_NATIONAL_ID",
        lambda text: _scan(
            text, "TR_NATIONAL_ID", _TCKN_PATTERNS, validate=_validate_tckn
        ),
    ),
    ("TR_VKN", _scan_vkn),
    ("SECRET", _scan_secret),
    ("DB_URI", _scan_db_uri),
)


def analyze_text(text: str, entities: tuple[str, ...]) -> list[Match]:
    requested = frozenset(entities)
    results: list[Match] = []
    for entity, scan in _RECOGNIZERS:
        if entity in requested:
            results.extend(scan(text))
    return results
