"""Presidio 2.2.364 patterns (MIT, Microsoft)."""

from __future__ import annotations

import base64
import binascii
import functools
import ipaddress
import re
import string
import sys
from typing import TYPE_CHECKING, NamedTuple

from .entities import PLACEHOLDER
from .iban_patterns import regex_per_country
from .normalize import normalize
from .suffixes import is_registrable

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import Any

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
    "TR_LICENSE_PLATE": "TR_LICENSE_PLATE",
    "SECRET": "SECRET",
    "DB_URI": "DB_URI",
    "CUSTOM": "CUSTOM",
}

_FLAGS = re.DOTALL | re.MULTILINE | re.IGNORECASE
_IBAN_FLAGS = re.DOTALL | re.MULTILINE
_TRAILING_PROSE = ".,;:!?)]}>"
_PHONE_REGIONS = ("US", "GB", "DE", "FR", "IL", "IN", "CA", "BR", "TR")
_PHONE_SCORE = 0.4
_PHONE_LENIENCY = 1
BARE_NUMBER = "BARE_NUMBER"
_BARE_DIGITS = re.compile(r"\d+")
_DECIMAL_LITERAL = re.compile(r"\d+\.\d+")
_DECIMAL_POINT = re.compile(r"\d\.|\.\d")
_TURKISH_SHAPES = re.compile(r"(?:90)?0?5\d{9}|0[2-4]\d{9}")
_NOT_DIGIT = re.compile(r"\D")
_PHONE_FORMAT = re.compile(r"[\s()+]")
# A context cue is a word of its own or one part of a key: not after a letter,
# or at a lower-to-upper camel join (`phone_number`, `contactPhone`, `alıcıVkn`).
_CUE_START = r"(?:(?<![^\W\d_])|(?-i:(?<=[a-zçğıöşü])(?=[A-Z])))"
_CUE_END = r"(?:(?![^\W\d_])|(?-i:(?<=[a-zçğıöşü])(?=[A-Z])))"
# A vCard line names the phone before its parameters: `TEL;TYPE=CELL:`.
_PHONE_CUE = re.compile(
    # Each cue's first letter; see `_SECRET_WORD`.
    r"(?=[cfgmnptw])"
    + _CUE_START
    + r"(?P<cue>telefon(?:um?)?|telephone|tel|phone|gsm|cep|mobile|mobil|cell|fax|whatsapp|call"
    r"|numara|num|no)(?:[\W_]{0,3}(?:number|numaras[ıi]))?"
    r"(?:\W{0,4}|(?<=tel)(?:;[^;:\s]*)+:)\Z",
    re.IGNORECASE,
)
# The longest cue it reads: `TEL;type=CELL;type=VOICE;type=pref:` is 35.
_PHONE_CUE_WINDOW = 48
# A generic number word after one of these names another kind of number:
# `order no`, `order_no`, `orderNo`.
_GENERIC_CUES = ("numara", "num", "no")
_NOT_PHONE_WORD = re.compile(
    r"(?=[cfikorst])"
    + _CUE_START
    + r"(?:order|sipari[şs]|fatura|invoice|ticket|ref|reference|sku|kod|code)"
    r"[^a-zçğıöşü]{0,3}\Z",
    re.IGNORECASE,
)
_IDENTIFIER_TAIL = re.compile(r"[A-Za-z0-9_.-]*\Z")
_LETTER = re.compile(r"[A-Za-z]")


class Match(NamedTuple):
    entity_type: str
    start: int
    end: int
    score: float
    label: str = ""


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


# No `=` in the local part, though RFC 5322 allows it. `SUPPORT_EMAIL=ops@x.com`
# is overwhelmingly a config line, not an address, and swallowing the `=` masked
# the variable name too — so a `.env` came back with `AWS_ACCESS_KEY_ID=<SECRET_1>`
# on one line and a bare `<EMAIL_1>` on the next. The cost is that a genuine
# address with `=` before the `@` is missed.
# Nor `/`, `?`, `#` or `&`: they separate path segments and query fields, so
# `https://host/u/jane@x.com` or `/work/jane@x.com/notes.txt` lost its host and
# folders to the placeholder. An address starts after the last of them.
_LOCAL = r"!$%'*+\-^_`{|}~\w"
# The run of local-part characters before each `@`, read once. The ported
# pattern tried every word start in the run, so `a.` repeated before an `@`
# was quadratic: 9 s at 40 KB.
_LOCAL_RUN = re.compile(rf"(?<![{_LOCAL}.])[{_LOCAL}.]+(?=@)")
_LOCAL_START = re.compile(rf"\b[{_LOCAL}]")
_DOMAIN = re.compile(r"\w+(?:-+\w+)*(?:\.\w+(?:-+\w+)*)+\b")
# `git@github.com:org/repo.git`: a git remote, not an address. Only a path that
# ends in `.git` counts, because `jane@x.com:Pa55/w0rd` combo lines, ports
# (`:8080/api`) and scp targets (`deploy@x.com:/var/www/`) look the same up to
# there; those stay masked.
_GIT_REMOTE = re.compile(r":[\w.~-]*/[\w.~/-]*")


def _validate_email(text: str) -> bool:
    # A DNS name is at most 253 characters; a longer run is no address, and
    # the suffix lookup is superlinear in it.
    domain = text.rpartition("@")[-1]
    return len(domain) <= 253 and is_registrable(domain)


_CARD_PATTERNS = _compile(
    (
        (
            r"\b(?!1\d{12}(?!\d))((4\d{3})|(5[0-5]\d{2})|(6\d{3})|(1\d{3})"
            r"|(3\d{3}))[- ]?(\d{3,4})[- ]?(\d{3,4})[- ]?(\d{3,5})\b",
            0.3,
        ),
        # 18 and 19 digits: Visa, Maestro, UnionPay and Discover, JCB, Mir.
        (
            r"\b(?:4\d{3}|5[06-8]\d{2}|6\d{3}|35(?:2[89]|[3-8]\d)|220[0-4])"
            r"(?:[- ]?\d{4}){3}[- ]?\d{2,3}\b",
            0.3,
        ),
        # Troy and the Mastercard 2-series, as the gateway reads them.
        (r"\b9792[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b", 0.3),
        (
            r"\b(?:222[1-9]|22[3-9]\d|2[3-6]\d{2}|27[01]\d|2720)"
            r"[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b",
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


# Each pattern's first character is a hex digit or a colon: the lookahead lets
# the engine pass any other position in one test; see `_SECRET_WORD`.
_IP_PATTERNS = _compile(
    (
        (
            r"(?=:)(?<![\w:])::(?:ffff(?::0{1,4})?:)?"
            r"(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
            r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)"
            r"(?:/(?:12[0-8]|1[01]\d|[1-9]?\d))?\b",
            0.6,
        ),
        (
            r"(?=[0-9A-Fa-f])(?<![\w:])(?:(?:[0-9A-Fa-f]{1,4}:){1,5}:(?:[0-9A-Fa-f]{1,4}:){0,4}"
            r"|(?:[0-9A-Fa-f]{1,4}:){6})"
            r"(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
            r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)"
            r"(?:/(?:12[0-8]|1[01]\d|[1-9]?\d))?\b",
            0.6,
        ),
        (
            r"(?=[0-9])\b(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\."
            r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\."
            r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\."
            r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)"
            r"(?:/(?:[0-2]?\d|3[0-2]))?\b",
            0.6,
        ),
        (
            r"(?=[0-9A-Fa-f:])(?<![\w:])(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
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


# The gateway's version cue. No "v": a glued "v1.2.3.4" never matches an
# address, and a lone one is a flag or a letter, "curl -v 203.0.113.9". A bare
# "ver" after a word is the Turkish verb: "izin ver 10.0.0.5" is an address.
# `==` is a pinned requirement only glued to a name holding a lowercase letter,
# `numpy==1.26.4.1`; not `ip == "203.0.113.7"` or `SERVER_IP==203.0.113.9`.
# "version" may end a name, as code writes one: `<AssemblyVersion>1.0.0.0`,
# `APP_VERSION = "2.3.1.0"`, `__version__ = "4.0.0.1"`.
_VERSION_CUE = re.compile(
    r"(?:(?:\b|_|(?-i:(?<=[a-z])(?=V)))version[\W_]{0,6}"
    r"|(?:\b(?:sürüm|ver\.)|(?<!\w\s)\bver)\W{0,2})\Z"
    r"|(?-i:[\w.-]*[a-z][\w.-]*)==\Z",
    re.IGNORECASE,
)
_VERSION_CUE_WINDOW = 16


def _scan_ip(text: str) -> list[Match]:
    return [
        found
        for found in _scan(text, "IP_ADDRESS", _IP_PATTERNS, invalidate=_invalidate_ip)
        if not _VERSION_CUE.search(
            text, max(0, found.start - _VERSION_CUE_WINDOW), found.start
        )
    ]


def _invalidate_ip(text: str) -> bool:
    if ":" not in text and text.isascii():
        # Without a colon only the dotted pattern matched, and it bounds each
        # octet and the prefix, so `ip_interface` adds only its refusal of a
        # leading zero, at ten times the cost. The prefix's `\d` also takes
        # other scripts' digits, which `ip_interface` refuses: those go to it.
        octets = text.partition("/")[0].split(".")
        if any(len(octet) > 1 and octet[0] == "0" for octet in octets):
            return True
        return octets[0] == "127" or octets == ["0", "0", "0", "0"]
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
# Nine digits with no separator are an SSN only right after a word that says so,
# or right after such an SSN in a list: `"ssn" : "536904399"`,
# `Social Security Number 536904399`, `SSNs: 536221234, 536904399`.
_BARE_SSN_PATTERNS = _compile(((r"(?<![\d-])[0-9]{9}(?![\d-])", 0.5),))
_SSN_CUE = re.compile(
    _CUE_START + r"(?:ssn|social[\W_]*security)(?:[\W_]*(?:number|no|num))?s?"
    r"[\W_]{0,8}\Z",
    re.IGNORECASE,
)
_SSN_CUE_WINDOW = 32
_SSN_LIST_GAP = re.compile(r"[\W_]{1,4}")


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


def _scan_ssn(text: str) -> list[Match]:
    cued: list[Match] = []
    for found in _scan(text, "US_SSN", _BARE_SSN_PATTERNS, invalidate=_invalidate_ssn):
        listed = cued and _SSN_LIST_GAP.fullmatch(text, cued[-1].end, found.start)
        if listed or _SSN_CUE.search(
            text, max(0, found.start - _SSN_CUE_WINDOW), found.start
        ):
            cued.append(found)
    return _scan(text, "US_SSN", _SSN_PATTERNS, invalidate=_invalidate_ssn) + cued


_TCKN_PATTERNS = _compile(
    (
        (r"\b[1-9][0-9]{10}\b", 0.3),
        (
            r"\b[1-9][0-9]{2}([ -])[0-9]{3}\1"
            r"(?:[0-9]{3}\1[0-9]{2}|[0-9]{2}\1[0-9]{3})\b",
            0.3,
        ),
    )
)


def _validate_tckn(text: str) -> bool:
    text = text.replace(" ", "").replace("-", "")
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
    r"(?=[tv])"
    + _CUE_START
    + r"(?:vkn|vergi(?:kimlik)?(?:no|numaras[ıi])?|tax(?:payer)?(?:id|no|number)?)"
    + _CUE_END,
    re.IGNORECASE,
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
# Lowercase, or broken over a line between groups. The strict pattern stays for
# every other IBAN: this one alone can pull a following word into the match.
_IBAN_SEPARATOR = r"(?:[ -]|[ ]*\r?\n[ ]*)?"
_IBAN_LOOSE_PATTERN = re.compile(
    rf"(?<![A-Z0-9])([A-Z]{{2}}[0-9]{{2}}(?:{_IBAN_SEPARATOR}[A-Z0-9]{{4}}){{2,6}})"
    rf"((?:{_IBAN_SEPARATOR}[A-Z0-9]{{4}})?)((?:{_IBAN_SEPARATOR}[A-Z0-9]{{1,3}})?)"
    r"(?![A-Z0-9])",
    _IBAN_FLAGS | re.IGNORECASE,
)
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


def _validate_iban(text: str) -> bool:
    value = re.sub(r"[ \r\n-]", "", text).upper()
    try:
        return _iban_check_digits(value) == value[2:4] and _iban_format_matches(value)
    except ValueError:
        return False


def _scan_iban(text: str) -> list[Match]:
    results: list[Match] = []
    matches = [*_IBAN_PATTERN.finditer(text), *_IBAN_LOOSE_PATTERN.finditer(text)]
    for match in matches:
        for group in reversed(range(1, len(match.groups()) + 1)):
            start = match.span(0)[0]
            end = match.span(group)[1] if match.span(group)[1] > 0 else match.span(0)[1]
            current = text[start:end]
            if current and _validate_iban(current):
                results.append(Match("IBAN_CODE", start, end, 1.0))
                break
    return results


def _phone_cue(text: str, start: int) -> bool:
    found = _PHONE_CUE.search(text, max(0, start - _PHONE_CUE_WINDOW), start)
    if found is None:
        return False
    if found.group("cue").lower() not in _GENERIC_CUES:
        return True
    return not _NOT_PHONE_WORD.search(
        text, max(0, found.start() - _PHONE_CUE_WINDOW), found.start()
    )


def _glued_to_identifier(text: str, start: int) -> bool:
    """claude-sonnet-4-5-20250929, build_7.20250929: the end of a name."""
    if not start or text[start - 1] not in "-_.":
        return False
    tail = _IDENTIFIER_TAIL.search(text, max(0, start - 65), start - 1)
    return tail is not None and bool(_LETTER.search(tail.group()))


# Digits, `+` and the library's punctuation other than its extension marks
# (`x`, `~`): no extension or letter to read, so the digits are the number's.
_PLAIN_NUMBER = re.compile(
    "[\\d+\uff0b\\-\u2010-\u2015\u2212\u30fc\uff0d-\uff0f \u00a0\u00ad\u200b\u2060"
    "\u3000()\uff08\uff09\uff3b\uff3d.\\[\\]/\u2053\u223c]*"
)
# The library reads a number as international when it starts with a `+`.
_LEADING_PLUS = re.compile("[^+\uff0b\\d]*[+\uff0b]")
# Three letters are read as digits too: "1-800-FLOWERS".
_SPELLED = re.compile(r"(?:.*?[A-Za-z]){3}")
_FIRST_GROUP_ONLY = re.compile(r"\(?\\1\)?")
_BACK_REFERENCE = re.compile(r"\\\d")
# The shortest number of every region has four digits; prefixes only add more.
_FEWEST_PHONE_DIGITS = 4
_ENOUGH_DIGITS = re.compile(rf"(?:\D*\d){{{_FEWEST_PHONE_DIGITS}}}")
# A `+` and a known country code read the same from every region.
_ANY_REGION = "*"
# Characters the library's candidate pattern cannot hold (it reads only digits,
# separators and the letters of `ext`, `anexo` and `int`, in any case), so the
# text can be cut at one: each side keeps it to look at, as before.
_PHONE_BREAK = re.compile(
    r"[\n\r!\"$%&'*<>?@\\^_`{|}b-df-hj-mp-ru-wyzB-DF-HJ-MP-RU-WYZ]"
)
_PHONE_ROW = 1_000
# What the walk remembers across rows, pieces and calls: a list repeats its
# numbers, and a candidate costs the walk 60 to 100 us. The memos hold about
# `_PHONE_MEMO_BYTES` together, an eighth of it for parts' readings, however
# much text a long-lived process (`shim watch`) reads.
_PHONE_MEMO_BYTES = 10 << 20
# Per entry, beyond its key and value: its slot in the memo, which takes 30 to
# 60 bytes as the table grows, and a margin for older Pythons' objects.
_PHONE_MEMO_ENTRY = 96
# What `is_valid_number` asks a national number to match one of.
_NUMBER_TYPES = (
    "premium_rate",
    "toll_free",
    "shared_cost",
    "voip",
    "personal_number",
    "pager",
    "uan",
    "voicemail",
    "fixed_line",
    "mobile",
)


class _Memo(dict):
    """A dict emptied once what it holds passes `budget` bytes."""

    def __init__(self, budget: int) -> None:
        super().__init__()
        self.budget = budget
        self.held = 0

    def keep(self, key: str, value: object, size: int) -> None:
        """Remember `value`, of `size` bytes, under `key`."""
        size += sys.getsizeof(key) + _PHONE_MEMO_ENTRY
        if self.held + size > self.budget:
            # ponytail: emptied when full, not least-recently-used.
            self.clear()
            self.held = 0
        self.held += size
        self[key] = value


class _PhoneRegion:
    """What the library's `parse` and `is_valid_number` need of a number
    written without `+` in one region, read from its metadata: after an
    optional country code and national prefix, the national number has a
    length and a pattern of one of the country's number types, and the
    national prefix is there when the number's format asks for it. It never
    refuses a number the library would accept; the library decides the rest."""

    def __init__(self, region: str) -> None:
        import phonenumbers

        def metadata(name: str) -> Any:
            # Every region named here has metadata; the stubs allow for none.
            return phonenumbers.PhoneMetadata.metadata_for_region(name)

        own = metadata(region)
        code = own.country_code
        main = metadata(phonenumbers.region_code_for_country_code(code))
        shapes: dict[int, list[str]] = {}
        for each in map(metadata, phonenumbers.COUNTRY_CODE_TO_REGION_CODE[code]):
            general = each.general_desc.possible_length
            for kind in _NUMBER_TYPES:
                desc = getattr(each, kind)
                if desc is not None and desc.national_number_pattern:
                    for length in set(desc.possible_length or general) & set(general):
                        shapes.setdefault(length, []).append(
                            desc.national_number_pattern
                        )
        self.shapes = {
            length: "|".join(f"(?:{shape})" for shape in dict.fromkeys(each))
            for length, each in shapes.items()
        }
        self.formats = "|".join(
            f"(?P<f{index}>"
            + (
                f"(?={rule.leading_digits_pattern[-1]})"
                if rule.leading_digits_pattern
                else ""
            )
            + f"(?:{rule.pattern})\\Z)"
            for index, rule in enumerate(main.number_format)
        )
        self.needs_prefix = [
            bool(rule.national_prefix_formatting_rule)
            and not rule.national_prefix_optional_when_formatting
            and not _FIRST_GROUP_ONLY.fullmatch(rule.national_prefix_formatting_rule)
            for rule in main.number_format
        ]
        self.code = str(code)
        self.dialling = re.compile(own.international_prefix)
        self.prefixes = [
            re.compile(pattern)
            for pattern in dict.fromkeys(
                each.national_prefix_for_parsing for each in (own, main)
            )
            if pattern
        ]
        # A transform that writes digits of its own leaves nothing to rule out.
        self.exact = all(
            _BACK_REFERENCE.fullmatch(each.national_prefix_transform_rule)
            for each in (own, main)
            if each.national_prefix_transform_rule
        )

    def _stripped(self, digits: str, start: int) -> set[int]:
        found = {start}
        for prefix in self.prefixes:
            match = prefix.match(digits, start)
            if match:
                found.add(match.end())
                found.update(
                    match.start(group) for group in range(1, prefix.groups + 1)
                )
        found.discard(-1)
        return found

    def _fits(self, digits: str, starts: set[int]) -> bool:
        for start in starts:
            shape = self.shapes.get(len(digits) - start)
            if shape is not None and _compiled(shape).fullmatch(digits, start):
                return True
        return False

    def admits(self, digits: str, written: str) -> bool:
        """`digits` as `reading` gives them; `written` is the text they were
        read from."""
        if digits.startswith("+"):
            # No country followed the `+`, so the library drops it and reads
            # the digits again: only an international prefix or this region's
            # code can then give them one.
            digits = digits[1:]
            if not (self.dialling.match(digits) or digits.startswith(self.code)):
                return False
        if not self.exact or self.dialling.match(digits):
            return True
        starts = self._stripped(digits, 0)
        if digits.startswith(self.code):
            for start in self._stripped(digits, len(self.code)):
                starts |= self._stripped(digits, start)
            return self._fits(digits, starts)
        if not self._fits(digits, starts):
            return False
        chosen = None if len(starts) > 1 else _compiled(self.formats).match(digits)
        if chosen is None or not self.needs_prefix[int(chosen.lastgroup[1:])]:
            return True
        # The library looks for the prefix in every digit as written: those
        # of an extension too, and not letters it read as digits.
        return len(self._stripped(_written_digits(written), 0)) > 1


@functools.lru_cache(maxsize=64)
def _written_digits(written: str) -> str:
    """The same text is asked of each region that needs its prefix."""
    import phonenumbers

    return phonenumbers.normalize_digits_only(written)


# The type unions are compiled on first use: North America's alone takes 8 ms.
# The nine regions' metadata gives 39 of them.
_compiled = functools.lru_cache(maxsize=128)(re.compile)
# A region's metadata is read when a candidate first needs it, as the library
# reads it: North America's shared code alone is 25 regions' modules.
_phone_region = functools.lru_cache(maxsize=len(_PHONE_REGIONS))(_PhoneRegion)


@functools.cache
def _phone_walk() -> Any:
    import phonenumbers
    from phonenumbers import phonenumbermatcher as library
    from phonenumbers import phonenumberutil as util

    readings = _Memo(_PHONE_MEMO_BYTES // 8)
    outcomes = _Memo(_PHONE_MEMO_BYTES - readings.budget)

    def reading(candidate: str) -> str:
        known = readings.get(candidate)
        if known is None:
            known = reading_of(candidate)
            readings.keep(candidate, known, sys.getsizeof(known))
        return known

    def reading_of(candidate: str) -> str:
        """What decides whether a region's parser accepts `candidate`, with
        the text itself: its digits, after a `+` when no known country follows
        it; "" when no region can, and `_ANY_REGION` when every region reads it
        alike."""
        digits = _NOT_DIGIT.sub("", candidate)
        if len(digits) < _FEWEST_PHONE_DIGITS and not _SPELLED.match(candidate):
            return ""
        number = candidate
        if not _PLAIN_NUMBER.fullmatch(number):
            # As `parse` reads it: without its extension, and its letters
            # dropped, or read as digits when there are three.
            _, number = util._maybe_strip_extension(
                util._extract_possible_number(number)
            )
            if _SPELLED.match(number):
                digits = util._normalize(number)
            else:
                digits = _NOT_DIGIT.sub("", number)
            if len(digits) < _FEWEST_PHONE_DIGITS:
                return ""
        if not digits.isascii():
            digits = phonenumbers.normalize_digits_only(digits)
        if not _LEADING_PLUS.match(number):
            return digits
        if digits[0] != "0" and any(
            int(digits[:size]) in phonenumbers.COUNTRY_CODE_TO_REGION_CODE
            for size in (1, 2, 3)
        ):
            return _ANY_REGION
        return "+" + digits

    class Walk(library.PhoneNumberMatcher):
        """The nine regions' matchers over one row, walked together.

        A matcher goes on from the end of its match, or of the candidate when
        it has none, so regions at the same place share the candidate. Its
        parts come in the library's order, and a region's match is the first
        part valid there; the search stops once every region has one."""

        def __init__(self, row: str) -> None:
            super().__init__(row, None, _PHONE_LENIENCY, max_tries=sys.maxsize)
            self.parsers: dict[str | None, Any] = {}
            self.verdicts: dict[tuple[str | None, int, str], bool] = {}
            self.spans: set[tuple[int, int]] = set()
            # A part of the row the library tries: its offset, its text and
            # `reading` of it. The whole candidate is the first.
            self.whole: tuple[int, str, str] = (0, "", "")
            self.open: list[str] = []
            self.waiting: list[str] | None = None
            self.found: dict[str, tuple[int, str]] = {}

        def read(self) -> set[tuple[int, int]]:
            paths = {0: list(_PHONE_REGIONS)}
            while paths:
                index = min(paths)
                names = paths.pop(index)
                match = library._PATTERN.search(self.text, index)
                if match is None:
                    continue
                start = match.start()
                # As the library's `_find` does; its stub leaves the pattern out.
                second = library._SECOND_NUMBER_START_PATTERN  # ty: ignore[unresolved-attribute]
                candidate = self._trim_after_first_match(second, match.group())
                found = ()
                if _ENOUGH_DIGITS.match(candidate) or _SPELLED.match(candidate):
                    found = self._outcome(candidate, start, names)
                ends = {name: end for name, _, end in found}
                for name in names:
                    paths.setdefault(start + ends.get(name, len(candidate)), []).append(
                        name
                    )
                self.spans.update((start + each, start + end) for _, each, end in found)
            return self.spans

        def _outcome(
            self, candidate: str, start: int, names: list[str]
        ) -> tuple[tuple[str, int, int], ...]:
            """Each region's match in `candidate`, relative to `start`. It is
            the same wherever the candidate recurs between the same characters
            with the same regions: a part's verdict looks one character out,
            the time-stamp check three."""
            end = start + len(candidate)
            before = self.text[max(start - 1, 0) : start]
            after = self.text[end : end + 3]
            # One string, so the memo can size it. Each neighbour's length
            # goes before it, as it may hold any character; region names and
            # candidates hold no NUL.
            key = "".join(
                (
                    chr(len(before)),
                    before,
                    chr(len(after)),
                    after,
                    ",".join(names),
                    "\0+" if (start, end) in self.spans else "\0-",
                    candidate,
                )
            )
            known = outcomes.get(key)
            if known is None:
                self.open, self.waiting, self.found = names, None, {}
                self._extract_match(candidate, start)
                known = tuple(
                    (name, offset - start, offset + len(part) - start)
                    for name, (offset, part) in self.found.items()
                )
                outcomes.keep(
                    key, known, sys.getsizeof(known) + sum(map(sys.getsizeof, known))
                )
            return known

        def _parse_and_verify(self, candidate, offset):
            part = offset, candidate, reading(candidate)
            first = self.waiting is None
            if first:
                self.whole = part
            elif part[2] == "" or part == self.whole:
                # No region reads a number in it, or every open region has
                # read it already, so nothing changes.
                return None
            waiting: list[str] = [] if first else self.waiting
            for name in self.open:
                if first and (offset, offset + len(candidate)) in self.spans:
                    # Another region read the whole candidate as a number;
                    # this one does or does not, and ends there either way,
                    # unless a part of it is a number here.
                    waiting.append(name)
                elif self._valid(name, part):
                    self._take(name, part)
            for name in () if first else waiting:
                if part != self.whole and self._valid(name, part):
                    whole = self._valid(name, self.whole)
                    self._take(name, self.whole if whole else part)
            self.open = [
                name
                for name in self.open
                if name not in self.found and name not in waiting
            ]
            self.waiting = [name for name in waiting if name not in self.found]
            return None if self.open or self.waiting else self

        def _take(self, name: str, part: tuple[int, str, str]) -> None:
            self.found[name] = part[:2]
            self.spans.add((part[0], part[0] + len(part[1])))

        def _valid(self, name: str, part: tuple[int, str, str]) -> bool:
            offset, text, how = part
            if how == "" or (
                how != _ANY_REGION and not _phone_region(name).admits(how, text)
            ):
                return False
            region = None if how == _ANY_REGION else name
            key = region, offset, text
            if key not in self.verdicts:
                if region not in self.parsers:
                    self.parsers[region] = library.PhoneNumberMatcher(
                        self.text, region, _PHONE_LENIENCY
                    )
                found = self.parsers[region]._parse_and_verify(text, offset)
                self.verdicts[key] = found is not None
            return self.verdicts[key]

    return Walk


def _phone_spans(text: str) -> set[tuple[int, int]]:
    """Every span the library's matcher finds for any of `_PHONE_REGIONS`.

    Nine matchers over a table of numbers cost 40 us a character, so a large
    one reached the hook's deadline and passed through unmasked. The walk
    reads each candidate once, asks a region's parser only where its metadata
    allows a number, and once one region has read a whole candidate as a
    number, asks the others only about its parts."""
    if not _BARE_DIGITS.search(text):
        return set()
    walk = _phone_walk()
    return {
        (offset + start, offset + end)
        for offset, row in _phone_rows(text)
        if _BARE_DIGITS.search(row)
        for start, end in walk(row).read()
    }


def _phone_rows(text: str) -> Iterator[tuple[int, str]]:
    """The text in parts of `_PHONE_ROW` characters or more, each cut at a
    `_PHONE_BREAK` that both keep: no candidate holds one, so every region's
    walk reaches it at the same place."""
    start = 0
    while len(text) - start > 2 * _PHONE_ROW:
        cut = _PHONE_BREAK.search(text, start + _PHONE_ROW)
        if cut is None:
            break
        yield start, text[start : cut.end()]
        start = cut.start()
    yield start, text[start:]


def _scan_phone(text: str) -> list[Match]:
    results: list[Match] = []
    # `deduplicate` of spans of one kind and score: in order of start, longest
    # first, a span inside one kept before it is dropped.
    furthest = -1
    for start, end in sorted(_phone_spans(text), key=lambda span: (span[0], -span[1])):
        if end <= furthest:
            continue
        furthest = end
        candidate = Match("PHONE_NUMBER", start, end, _PHONE_SCORE)
        raw = text[start:end]
        number = raw.lstrip("([" + string.whitespace)
        if number != raw and not {")", "]"} & set(number):
            start, raw = end - len(number), number
            candidate = candidate._replace(start=start)
        if (
            _DECIMAL_LITERAL.fullmatch(raw)
            or _DECIMAL_POINT.fullmatch(text, max(0, start - 2), start)
            or _DECIMAL_POINT.fullmatch(text, end, end + 2)
        ):
            continue
        bare = _BARE_DIGITS.fullmatch(raw)
        turkish = _TURKISH_SHAPES.fullmatch(_NOT_DIGIT.sub("", raw))
        if not (turkish or _phone_cue(text, start)):
            # Digits joined by "-", "_" or "." can end a name; a space, a
            # bracket or a "+" makes a phone: "musteri-0532 123 45 67".
            if _glued_to_identifier(text, start) and not _PHONE_FORMAT.search(raw):
                continue
            if bare:
                candidate = candidate._replace(entity_type=BARE_NUMBER)
        results.append(candidate)
    return results


_LEGACY_KEY = (
    r"password|passwd|pwd|api[_-]?key|secret|token|db[_-]?pass|postgres_password"
)
_TURKISH_PASSWORD = r"[şs]ifre(?:si|m|n|niz)?|parola(?:s[ıi]|m|n|n[ıi]z)?"
_PASSWORD_WORDS = r"password|passwd|pwd|secret|" + _TURKISH_PASSWORD
_SECRET_KEY = (
    _LEGACY_KEY
    + r"|(?:access|private|signing|encryption|secret)[_-]?key"
    + r"|credentials?|auth[_-]?token|pass|"
    + _TURKISH_PASSWORD
)
# A secret word is one segment of a key: between non-letters or at a camel join.
_WORD_START = r"(?:(?<![^\W_])|(?-i:(?<=[a-z0-9])(?=[A-Z])))"
# Or glued onto a prefix in its case: `PGPASSWORD`, `ACCESSTOKEN`, `DBPassword`,
# `registrytoken`. Never `pass`, which ends `BYPASS`, `COMPASS` and `LOWPASS`;
# `DBPASS` is a key of its own.
_GLUED = r"(?-i:(?<=[A-Z0-9])(?=[A-Z])|(?<=[a-z0-9])(?=[a-z]))"
_GLUED_WORD = (
    r"token|(?:api|access|private|signing|encryption|secret)[_-]?key|" + _PASSWORD_WORDS
)
# A word ends where no letter follows, `PASSWORD2` and `PASSWORD_PROD` too, or
# at a camel join. The lookahead holds every word's first letter: the engine
# skips a position no word starts at in one test, where trying each assertion
# in turn made a scan of 200,000 characters take 19 ms instead of 2.
_SECRET_WORD = (
    r"(?=[acdepstş])(?:" + _WORD_START + r"(?:" + _SECRET_KEY + r")"
    r"|" + _GLUED + r"(?:" + _GLUED_WORD + r"))"
    r"(?:(?![^\W\d_])|(?-i:(?<=[a-z])(?=[A-Z])))"
)
_SECRET_NAME = re.compile(_SECRET_WORD, re.IGNORECASE)
# A key whose last word names a password: every value under it is one, digits
# included, where `TOKEN_TTL=86400` stays configuration.
_PASSWORD_KEY = re.compile(
    r"(?:(?:" + _WORD_START + r"|" + _GLUED + r")(?:" + _PASSWORD_WORDS + r")"
    r"|" + _WORD_START + r"pass)\d*\Z",
    re.IGNORECASE,
)
# A key name ending in a legacy key.
_LEGACY_NAME = re.compile(r"(?<![\w-])(?:" + _LEGACY_KEY + r")\Z", re.IGNORECASE)
# A quote around a key or a value may be escaped, as in JSON held in a JSON
# string. A value an escaped quote opened is read one escape level down, so
# `\\\"` is a quote inside it and a bare quote cannot be. A value an unescaped
# quote opened runs to its unescaped closing quote, which a word never follows.
# When there is no such quote six characters in, it runs, as before, to the
# first quote that is: `password="abcdef\" other="x"` ends at the backslash.
_QUOTED_VALUE = (
    r"(?P<quote>(?P<escaped>\\)?(?P<mark>[\"']))(?P<quoted>(?(escaped)"
    r"(?:\\?(?!(?P=mark))[^\r\n\\]|\\\\(?:\\[^\r\n]|[^\r\n\\])){6,}?"
    r"|(?:(?:(?!(?P=mark))[^\r\n\\]|\\[^\r\n]){6,}(?=(?P=mark)(?!\w))"
    r"|[^\r\n]{6,}?)))(?P=quote)"
)
# A typed declaration's value is its initializer, a literal after the type:
# `db_password: str = "..."`, `const DB_PASSWORD: &str = "..."`,
# `password?: string = "..."`, `db_password: int = 12345678`.
_SECRET_ASSIGNMENT = re.compile(
    _SECRET_WORD + r"(?P<tail>[\w.-]{0,64})(?:\\?[\"'])?\s*"
    r"(?:\??:[ \t]*(?P<type>(?:[\w.\[\]<>,|?&'() \t]|(?<=\w)=(?=\w)){1,40}?)"
    r"[ \t]*=(?=[ \t]*(?:\\?[\"']|\d))|[=:])\s*"
    r"(?:" + _QUOTED_VALUE + r"|(?P<bare>[^\s,}\]\"']{6}))",
    re.IGNORECASE,
)
# A type is written in words, and spaced from its `=`; anything else after the
# key may be the password's start.
_TYPE_WORD = re.compile(r"[A-Za-z]+(?:_[A-Za-z]+)*")
_BARE_END = re.compile(r"[\s,}\]\"']")
# The gateway's key and value with a space alone between them: the key starts a
# word and a bare value carries a digit or a symbol, so "token budget" is prose,
# and so is "rotate the token weekly.": a sentence's last mark is no symbol.
# Unlike the gateway, the space never crosses a line: "# rotate the token" over
# a line of code is not an assignment, and a command's next `--option` is no
# value. A value attribute after the key holds the value:
# `password value=<SECRET_1>`. A key that is an attribute's value, as in
# `name="password"` or `name=password`, or an element's name, as in
# `<password encrypted="false">`, is followed by the element's next attribute,
# which is no value: `id=pwfield1` stays.
_SPACED_ASSIGNMENT = re.compile(
    r"(?=[adpst])(?<![^\W_])(?:" + _LEGACY_KEY + r")(?P<closed>[\"'])?[ \t]+"
    r"(?:(?P<quote>[\"'])(?P<quoted>[^\r\n]{6,}?)(?P=quote)"
    r"|(?!--)(?=[^\s,}\]\"']*(?:[0-9!#$%&*+/<=>@^_|~-]|[.;?](?=[^\s,}\]\"'])))"
    r"(?P<bare>[^\s,}\]\"']{6,}))",
    re.IGNORECASE,
)
# The element's next attribute, with its value: `aGVsbG8=` is a padded value.
_ATTRIBUTE_NAME = re.compile(r"[A-Za-z_][\w.:-]*=(?=[\w\"'#/\\])")
# `<add key="DbPassword" value="..."/>`, `<input type="password" value="...">`,
# `<meta name="api-key" content="...">`: the secret's name and its value are
# two attributes of one element, in either order, and others may sit between.
# Either may carry a prefix, as `data-name` does, and a value attribute's value
# may be unquoted: it runs, as a browser reads it, to whitespace or `>`. An
# attribute follows whitespace, so `value=value=` is one value, read once.
# `:value` and `th:value` bind an expression, not a value.
_NAME_ATTRIBUTE = (
    r"(?<![\w:.-])(?:[\w.:-]*-)?(?:key|name|type|id)\s*=\s*"
    r"(?P<open>(?:\\?[\"'])?)"
    r"(?P<key>[\w.:-]{0,64}?" + _SECRET_WORD + r"[\w.:-]{0,64})(?P=open)"
)
_VALUE_ATTRIBUTE = (
    r"(?<!\S)(?:[\w.:-]*-)?(?:value|content)\s*=\s*(?P<quote>\\?[\"'])?"
    r"(?P<value>(?(quote)(?:(?!(?P=quote))[^\r\n])*|[^\s>]*))(?(quote)(?P=quote))"
)
_VALUE_ATTRIBUTE_AT = re.compile(_VALUE_ATTRIBUTE, re.IGNORECASE)
_OTHER_ATTRIBUTES = (
    r"(?:\s+[\w:.-]+(?:\s*=\s*(?:\\?\"[^\"\r\n]*\"|\\?'[^'\r\n]*'|[^\s\"'>][^\s>]*))?)"
    r"{0,8}?\s+"
)
_ATTRIBUTE_PAIRS = (
    re.compile(_NAME_ATTRIBUTE + _OTHER_ATTRIBUTES + _VALUE_ATTRIBUTE, re.IGNORECASE),
    re.compile(
        _VALUE_ATTRIBUTE + r"(?=" + _OTHER_ATTRIBUTES + _NAME_ATTRIBUTE + r")",
        re.IGNORECASE,
    ),
)
# `<password>...</password>`, `<entry key="db.password">...</entry>`: an element
# a secret word names, by its tag or by its name or key attribute, holds the
# secret, which may sit on lines of its own.
_ELEMENT_CONTENT = (
    r"(?:\s[^<>]{0,256}?)?(?<!/)>\s*(?P<value>[^\s<](?:[^<]*[^\s<])?)\s*</(?P=tag)>"
)
_ELEMENT_VALUES = (
    re.compile(
        r"<(?P<tag>(?P<key>[\w.:-]{0,64}?"
        + _SECRET_WORD
        + r"[\w.:-]{0,64}))"
        + _ELEMENT_CONTENT,
        re.IGNORECASE,
    ),
    re.compile(
        r"<(?P<tag>[\w.:-]{1,64})\s[^<>]{0,256}?(?<![\w:.-])(?:key|name)\s*=\s*"
        r"(?P<open>[\"']?)(?P<key>[\w.:-]{0,64}?" + _SECRET_WORD + r"[\w.:-]{0,64})"
        r"(?P=open)" + _ELEMENT_CONTENT,
        re.IGNORECASE,
    ),
)
# A name field naming a secret, then, before the next name field or the end of
# its object or element, the value field holding it: a Kubernetes or ECS `env`
# entry, AWS SSM's `Name` and `Value` as JSON or as the CLI's options, a
# Terraform variable's `default`. Not `th:value`, which binds an expression,
# nor an element's `name=` attribute, which `_ATTRIBUTE_PAIRS` reads.
_NAME_FIELD = (
    r"(?:(?<![\w-])\\?[\"']?(?:name|key)\\?[\"']?(?:\s*:|\s+=)"
    r"|(?<!\S)--(?:name|key)[ \t=])"
)
_PAIRED_VALUE = re.compile(
    # Each first character a match can start with; see `_SECRET_WORD`.
    r"(?=[\\\"'nkv-])(?:" + _NAME_FIELD + r"|(?<![\w-])variable)\s*(?P<open>\\?[\"']?)"
    r"(?P<key>[\w./:-]{0,64}?" + _SECRET_WORD + r"[\w./:-]{0,64})(?![\w./:-])(?P=open)"
    r"(?:(?!" + _NAME_FIELD + r")[^}<>]){0,256}?"
    r"(?:(?<![\w:.-])\\?[\"']?(?:value|default)\\?[\"']?\s*[:=]"
    r"|(?<!\S)(?P<option>--value)[ \t=])[ \t]*"
    r"(?:" + _QUOTED_VALUE + r"|(?P<bare>(?!\\?[\"'])[^\r\n]*|))",
    re.IGNORECASE,
)
# A key that a `;`, `&`, `?` or `|` puts in a list of fields: a connection
# string's option, a URL's parameter, a log line's field. Only there does the
# separator end a value. In a .env, YAML or properties value it can be a
# character of the password. A key has 64 characters at most, so the separator,
# a space, the key and its `=` fit in `_KEY_REACH`.
_LISTED_KEY = re.compile(r"(?P<separator>[;&?|])[ \t]?[\w.-]{1,64}=\Z")
_KEY_REACH = 67
# The list's next field, whose name may hold spaces (`;Connect Timeout=30`).
_NEXT_FIELD = {
    separator: re.compile(re.escape(separator) + r"(?=[ \t]?[A-Za-z_][\w. -]{0,64}=)")
    for separator in ";&|"
}
# What a bare value may end with that belongs to the text around it, and what
# opens each closing mark.
_VALUE_MARKS = ";&|)>`}]"
_OPENERS = {")": "(", ">": "<", "`": "`", "}": "{", "]": "["}
_CLOSING_MARK = re.compile(r"[)>`}\]]")
_UNQUOTED_END = re.compile(r"[\s>]")
_LINE_BREAK = re.compile(r"[\r\n]")
# `${NAME:?message}` and `${NAME?message}` stop the shell when NAME is unset:
# the text after `?` is the message, never a value. A `:-` default can be one.
# An environment lookup is one in every language that names it: Go's
# `os.Getenv(`, Java's `System.getenv(`, C#'s, Rust's and Vite's.
_REFERENCE = re.compile(
    r"\$\{\w+(?::?\?[^}]*)?\}?|\$[A-Za-z_]\w*|%\w+%"
    r"|(?:os\.environ\W|process\.env[.\[]|import\.meta\.env\.|(?:os\.)?getenv\("
    r"|os\.Getenv\(|System\.getenv\(|Environment\.GetEnvironmentVariable\("
    r"|(?:std::)?env(?:::var)?\().*"
)
_TYPE_NAME = re.compile(
    r"(?:string|String|number|boolean|object|unknown|undefined|SecretStr"
    r"|Optional\[\w*)[;,?|)>\[\]:]*"
)
_QUALIFIER = re.compile(
    r"(?:[_.-]|(?-i:(?<=[a-z0-9])(?=[A-Z])))"
    r"(?:ids?|type|name|header|count|limit|regex|service)\Z",
    re.IGNORECASE,
)
_CODE_PATH = r"[A-Za-z_]+(?:\??\.[A-Za-z_]+)*"
_CODE_VALUE = re.compile(
    _CODE_PATH + r"(?:[(\[<]|\([A-Za-z_.]*\)|\[[A-Za-z_.]*\]|<[A-Za-z_.<>]*>)[;)]*"
    r"|(?:[A-Za-z_]+\??\.)+[A-Za-z_]+[;)]*"
)
_OPEN_CODE_VALUE = re.compile(_CODE_PATH + r"[(\[<][A-Za-z_.=(\[<]*")
_TYPE_VALUE = re.compile(r"(?P<name>(?:[A-Z][a-z]+){2,})(?P<end>[?;)>]*)")
_NAME_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")
# An identifier as people write one: in one case, or in camel case with every
# capital opening a word.
_NAME = re.compile(
    r"[A-Z0-9_]+|[a-z0-9_]+|_*(?:[a-z][a-z0-9]*|[A-Z][a-z0-9]+)(?:[A-Z][a-z0-9]+)+"
)
_NAME_RUN = re.compile(r"\w+")
_MAX_SHAPED_VALUE = 1_024
_PATH_VALUE = re.compile(
    r"(?:~|\.{1,2})/\S*|/[a-z0-9._/-]+|/\S*\.[A-Za-z][A-Za-z0-9]{0,4}"
)
_URL_VALUE = re.compile(r"https?://", re.IGNORECASE)
_URL_USERINFO = re.compile(r"(?i)\b(?:https?|ftp|wss?)://(?P<userinfo>[^\s/?#'\"<>]+)@")
# A `+driver` suffix is SQLAlchemy's (`postgresql+asyncpg`) or MongoDB's `+srv`.
_DB_URI = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb|redis(?:s)?|mssql|sqlserver"
    r"|clickhouse)(?:\+\w+)?://([^\s'\"<>]*)"
)
# A password in the userinfo of a URI of any other scheme (amqp, smtp, ssh...).
# The scheme is read from where a run of scheme characters starts, so the scan
# reads each run once: from every `.` inside one, as `\b` would, is quadratic.
# A diff's `+` or `-` is read as part of the scheme.
_URI_PASSWORD = re.compile(
    r"(?i)(?<![a-z0-9+.-])[a-z0-9+.-]+://"
    r"(?P<userinfo>[^\s/?#'\"<>:]*:(?P<password>[^\s/?#'\"<>]+))@"
)
# An image pinned by digest (`docker://alpine:3.8@sha256:...`): no host is that.
_IMAGE_DIGEST = re.compile(r"(?i)sha(?:256|512):[0-9a-f]{64}")
# A template's placeholder for a value: `<your-api-token>`, `<YOUR_API_TOKEN>`.
_TEMPLATE_VALUE = re.compile(r"<(?:[a-z]+(?:[-_][a-z]+)*|[A-Z]+(?:[-_][A-Z]+)*)>")
_NOT_A_CREDENTIAL = re.compile(r"[….*•:]+|(?:\$\{\w+\}|\{\w*\}|\$\w+|%\w+%|:)+")
# An environment lookup is no secret, but the literal it falls back to, or has
# added to it, is one when the variable it reads or the key it is assigned to
# names a secret: `os.getenv("DB_PASSWORD", "...")`, `process.env.X || "..."`,
# `env::var("X").unwrap_or_else(|_| "...".into())`, `ENV.fetch("X", "...")`,
# `DB_PASSWORD=${DB_PASSWORD:?}...`. A default argument counts only inside a
# call that takes one, still open (`call`): after a closed call, `process.env.X`
# or `ENV["X"]`, a comma leads to the next argument or entry, never a default.
# A shell default may be one variable, `${X:-${Y}}...`. Text a URI, a path or a
# name joins to a variable is no literal of its own: `${DB_PASS}@db.internal`,
# `${HOME}/workspace`, `${SECRET_PREFIX}-database`, `${SECRET_KEY}_backup`.
# The windows are bounded: each start reads at most a line's 256 characters on.
_ENV_FALLBACK = re.compile(
    r"(?=[egiopsES$])(?:(?<![\w.])"
    r"(?:(?P<call>(?:os\.)?getenv\(|(?:os\.)?environ\.get\("
    r"|System\.getenv\(\)\.getOrDefault\(|ENV\.fetch\()"
    r"|os\.Getenv\(|System\.getenv\(|Environment\.GetEnvironmentVariable\("
    r"|(?:std::)?env::var\(|process\.env[.\[]|import\.meta\.env\.|ENV\[|\$_ENV\[)"
    r"[ \t]*(?:key=)?(?P<mark>[\"']?)(?P<name>\w{1,64})(?P=mark)"
    r"(?:(?(call)[ \t]*,[ \t]*(?:\r?\n[ \t]*)?(?:default=)?|(?!))"
    r"|[)\]]{0,2}(?:\.ok\(\))?"
    r"(?:[ \t]*(?:\|\||\?\?|\?:|\+|or\b)[ \t]*(?:\r?\n[ \t]*)?"
    r"|\.(?:unwrap_or|orElse)\(|\.unwrap_or_else\(\|\w*\|[ \t]*))"
    r"(?:String::from\()?"
    r"(?P<quote>[\"'`])(?P<value>(?:(?!(?P=quote))[^\r\n\\]|\\[^\r\n]){6,256})(?P=quote)"
    r"|\$\{(?P<shell>\w{1,64})"
    r"(?:[:?+=-](?:[^}$\r\n]|\$(?!\{)|\$\{\w{1,64}\}){0,256})?\}"
    r"(?P<tail>[^\s\"'`;|&<>(){}$@:/?#=,.%_-][^\s\"'`;|&<>(){}$]{5,255}))"
)
# A literal glued to a variable carries a digit or a symbol, as a spaced value
# does: `${API_TOKEN_VERSION}release` is a name.
_GLUED_LITERAL = re.compile(r"[0-9!%*+=@^_~.-]")
# The key a lookup is assigned to, or the variable whose `:-` default it is. A
# call between them takes the lookup's value as one argument of its own:
# `password = hash_password(os.getenv("DEFAULT_ROLE", "..."))`.
_ASSIGNED_KEY = re.compile(
    r"(?:\$\{(?P<outer>\w{1,64}):?-"
    r"|(?P<key>[\w.-]{1,64})[\"']?[ \t]*(?::=|\??:|=)[ \t]*)\Z"
)
# A `:-` default that is another variable: `${APP_TOKEN:-${FALLBACK_TOKEN}}`.
_DEFAULT_REFERENCE = re.compile(r"-\$\{\w+\}\}")
# A key with no value is matched too, so the scan moves past it once: the `?`s
# inside it start the same failed key, and retrying each was quadratic.
_QUERY_PARAMETER = re.compile(r"[?&;](?P<key>[^=&;#]*)(?:=(?P<value>[^&;#]+))?")
_AUTHORITY_END = re.compile(r"[/?#]")
# Vendor tokens. The JWT alternative they had is read in `_vendor_tokens`.
# A token glued to a word is part of an identifier, but one right after an
# escape written as text (JSON or a log line: `\nxoxb-...`) is a token. `rk_`
# ends words (benchmark_test_..., network_test_...), so a Stripe restricted key
# glued to a letter is left; a secret key `sk_` is read anywhere, as in 1.2.2.
_AFTER_ESCAPE = r"(?<=\\[nrtfbv])|"
_VENDOR_TOKEN = re.compile(
    r"(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"sk_(?:live|test)_[A-Za-z0-9]{16,}|"
    rf"(?:{_AFTER_ESCAPE}(?<![A-Za-z]))rk_(?:live|test)_[A-Za-z0-9]{{16,}}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
    r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|"
    rf"(?:{_AFTER_ESCAPE}(?<![\w-]))"
    r"(?:(?:xox[abposr]|xapp)-[0-9A-Za-z-]{10,}|AIza[0-9A-Za-z_-]{35}|"
    r"github_pat_[0-9A-Za-z_]{22,}|gl(?:pat|rt|dt|ptt|ft|soat)-[0-9A-Za-z_-]{20,}|"
    r"npm_[0-9A-Za-z]{36}|hf_[0-9A-Za-z]{30,})(?![\w-]))"
)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_NOT_TOKEN = re.compile(r"[^A-Za-z0-9_-]")


def _depth(text: str, start: int, end: int, closer: str) -> int:
    """How many of what `closer` closes `text[start:end]` leaves open."""
    opener = _OPENERS[closer]
    if opener == closer:
        return text.count(opener, start, end) % 2
    return text.count(opener, start, end) - text.count(closer, start, end)


def _value_end(text: str, start: int, end: int) -> int:
    """Where a bare value read from `start` ends, `end` being where its run of
    value characters does. `text` is the literal text, so a mark the source
    does not write as itself is NUL and ends nothing. A listed value ends at
    its list's next field, or at its list's separator among the marks it ends
    with. Otherwise a value inside brackets or a backtick its line left open
    ends before the closing marks it ends with that close them, when what it
    keeps is balanced. A password may hold any of these characters, so nothing
    else ends it; on a line whose marks do not balance, as in
    `(x (password=abc123)`, a closing mark of its own can stay outside. A value
    longer than `_MAX_SHAPED_VALUE` keeps its run, which bounds the work for
    every value read inside another."""
    listed = _LISTED_KEY.search(text, max(0, start - _KEY_REACH), start)
    separator = listed["separator"].replace("?", "&") if listed else ""
    if separator:
        reach = min(end, start + _MAX_SHAPED_VALUE + 1)
        found = _NEXT_FIELD[separator].search(text, start, reach + _KEY_REACH)
        if found and found.start() < end:
            return start + len(text[start : found.start()].rstrip(separator))
    if end - start > _MAX_SHAPED_VALUE:
        return end
    marks = start + len(text[start:end].rstrip(_VALUE_MARKS))
    if separator and (cut := text.find(separator, marks, end)) >= 0:
        return cut
    first = _CLOSING_MARK.search(text, marks, end)
    if first:
        closer = first.group()
        line = max(
            text.rfind("\n", max(0, start - _MAX_SHAPED_VALUE), start) + 1,
            start - _MAX_SHAPED_VALUE,
        )
        opened = _depth(text, line, start, closer)
        if opened > 0:
            closing = [i for i in range(first.start(), end) if text[i] == closer]
            cut = closing[max(len(closing) - opened, 0)]
            if not _depth(text, start, cut, closer):
                return cut
    return end


def _quoted_end(text: str, start: int, end: int) -> int:
    """Where a value quoted from `start` ends, a quote having closed it at
    `end`. `text` is the literal text: a value whose source writes its opening
    quote ends only at a quote its source writes, so a `%22` inside it is the
    password's. With no such quote on its line, it runs to the line's end."""
    mark = text[start - 1]
    if mark not in "\"'" or text[end] == mark or text[end : end + 2] == "\\" + mark:
        return end
    close = text.find(mark, end)
    line = _LINE_BREAK.search(text, end, len(text) if close < 0 else close)
    if line:
        return line.start()
    return len(text) if close < 0 else close


def _attribute_value(text: str, match: re.Match[str]) -> tuple[int, int]:
    """The span of a value attribute's value, ended in the literal `text`."""
    start, end = match.span("value")
    if match["quote"]:
        return start, _quoted_end(text, start, end)
    found = _UNQUOTED_END.search(text, start)
    return start, found.start() if found else len(text)


_SECRET_PATTERNS: tuple[
    tuple[re.Pattern[str], str | None, float, Callable[[str, int, int], int] | None],
    ...,
] = (
    (
        re.compile(
            # Any PEM or PGP private key, and the SSH2 form's `---- BEGIN `.
            r"(?P<open>-----|---- )BEGIN "
            r"(?P<key_type>(?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)(?P<close>-----| ----)"
            r"[\s\S]*?(?:(?P=open)END (?P=key_type)(?P=close)|\Z)"
        ),
        None,
        0.99,
        None,
    ),
    (
        re.compile(
            r"https://(?:hooks\.slack\.com/services|"
            r"discord(?:app)?\.com/api/webhooks)/[^\s'\"]+",
            re.IGNORECASE,
        ),
        None,
        0.99,
        _trim_trailing_prose,
    ),
    (
        re.compile(
            r"(?<![\w-])AccountKey=(?P<value>[A-Za-z0-9+/]{64,}={0,2})",
            re.IGNORECASE,
        ),
        "value",
        0.97,
        None,
    ),
    (
        re.compile(
            r"(?<![\w-])[\"']?(?:proxy-)?authorization[\"']?\s*:\s*[\"']?(?:basic|bearer)\s+"
            r"(?P<value>[A-Za-z0-9._~+/=-]{6,})",
            re.IGNORECASE,
        ),
        "value",
        0.97,
        None,
    ),
    (
        re.compile(
            r"--password(?:=|\s+)(?P<quote>[\"'])(?P<value>[^\r\n]{6,}?)(?P=quote)",
            re.IGNORECASE,
        ),
        "value",
        0.97,
        _quoted_end,
    ),
    (
        re.compile(r"--password(?:=|\s+)(?P<value>[^\s]+)", re.IGNORECASE),
        "value",
        0.97,
        _value_end,
    ),
)


def _words(name: str) -> set[str]:
    return {word.lower() for word in _NAME_WORD.findall(name)}


def _a_type_of(key: str, value: str, after: str) -> bool:
    found = _TYPE_VALUE.fullmatch(value)
    if found is None:
        return False
    ended = bool(found["end"]) or after == ","
    return ended and bool(_words(found["name"]) & _words(key))


def _as_decoded(value: str, as_written: bool) -> str:
    """A value of the source text as written is judged as it decodes, once:
    its `%24%7BDB_PASS%7D` is the reference `${DB_PASS}`, as the decoded text's
    is. Only a value that can be a reference is decoded, so its decoding stays
    far inside the input limit."""
    if not as_written or len(value) > _MAX_SHAPED_VALUE:
        return value
    return value if value.isascii() and "%" not in value else normalize(value).text


def _not_a_secret(
    value: str, new_key: str | None, quoted: bool, typed: bool, after: str
) -> bool:
    shaped = len(value) <= _MAX_SHAPED_VALUE
    if shaped and (
        _REFERENCE.fullmatch(value)
        or PLACEHOLDER.fullmatch(value.rstrip("`*.;:!?)"))
        or _TEMPLATE_VALUE.fullmatch(value)
        or _TYPE_NAME.fullmatch(value)
    ):
        return True
    if new_key is None:
        return False
    password = _PASSWORD_KEY.search(new_key) is not None
    code = not quoted and (
        _CODE_VALUE.fullmatch(value)
        or (after in ('"', "'", ",", "]") and _OPEN_CODE_VALUE.fullmatch(value))
        or (typed and _a_type_of(new_key, value, after))
    )
    if password:
        # A password may be any characters. A random one reads as digits, a
        # path or `a.b` often enough, and as code written in names almost
        # never: `config.db_password`, `settings["DB_PASSWORD"]`.
        code = code and all(map(_NAME.fullmatch, _NAME_RUN.findall(value)))
    return bool(
        _QUALIFIER.search(new_key)
        or _URL_VALUE.match(value)
        or (
            shaped
            and (
                code
                or (
                    not password
                    and (_BARE_DIGITS.fullmatch(value) or _PATH_VALUE.fullmatch(value))
                )
            )
        )
    )


# The name a key sits in: `db_password` around `password`.
_KEY_NAME_START = re.compile(r"\w{0,64}\Z")
_OPERAND_END = re.compile(r"[ \t]*(?:[,)}]|\?\?|\|\|)")
_CODE_OPERATOR = re.compile(r"[ \t]*\?\?")
_CAMEL_NAME = re.compile(r"[a-z]+(?:[A-Z][a-z]+)+")
_RECEIVER = re.compile(r"(?<![\w.])(?:self|this)\Z")


def _handed_on(text: str, key: int, start: int, end: int, separator: str) -> bool:
    """Code handing a variable on: `password=password,`, `self.token = token`,
    `{ Password = password ?? "" }`, `user.password = hashedPassword;`. A
    config file's unquoted value is the password itself, `PASSWORD=password`,
    `{user: admin, password: password}` and a `.properties` line's
    `db.password = adminPassword` too, so a bare name counts only where code
    assigns it: the key's own name as an argument or an operand, or a member
    set in a statement, one `;` ends or `self` or `this` owns, from the key's
    own name or from a camel-case name holding the key's word (`;` only)."""
    if "=" not in separator or end - start > _MAX_SHAPED_VALUE:
        return False
    value = text[start:end]
    name = value.rstrip(";)")
    own = _NAME_RUN.match(text, key)
    if own is None or not name.isidentifier():
        return False
    member = text[key - 1 : key] == "." and separator == " = "
    statement = member and value.endswith(";")
    if name == own[0]:
        return bool(
            ")" in value
            or _OPERAND_END.match(text, end)
            or statement
            or (member and _RECEIVER.search(text, max(0, key - 6), key - 1))
        )
    if name.lower() == own[0].lower():
        return _CODE_OPERATOR.match(text, end) is not None
    return bool(
        statement and _CAMEL_NAME.fullmatch(name) and _words(name) & _words(own[0])
    )


def _userinfos(
    pattern: re.Pattern[str], text: str, literal: str
) -> Iterator[re.Match[str]]:
    """The userinfo ends only at a mark its source writes, so a password's
    `%2F` does not end it; the `://` before it may be encoded too, as in a
    URL inside a query. So both are read: `text` and `literal`."""
    for view in (text,) if literal == text else (text, literal):
        if "://" in view:
            yield from pattern.finditer(view)


def _scan_secret(text: str, literal: str | None = None) -> list[Match]:
    """`literal` is `text` with NUL for each character its source does not write
    as itself: a value is cut only at a mark the source writes."""
    literal = text if literal is None else literal
    results: list[Match] = []
    for pattern, value_group, score, trim in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(value_group) if value_group else match.span()
            if trim is not None:
                end = trim(literal, start, end)
            if start < end:
                results.append(Match("SECRET", start, end, score))
    results.extend(Match("SECRET", *span, 0.99) for span in _vendor_tokens(text))
    for match in _ENV_FALLBACK.finditer(text):
        group = "value" if match["value"] is not None else "tail"
        assigned = _ASSIGNED_KEY.search(text, max(0, match.start() - 80), match.start())
        names = (
            match["name"] or match["shell"],
            assigned and (assigned["outer"] or assigned["key"]),
        )
        key = next((name for name in names if name and _SECRET_NAME.search(name)), "")
        value = match[group]
        if (group == "tail" and not _GLUED_LITERAL.search(value)) or (
            # A JavaScript template literal that interpolates is code.
            match["quote"] == "`" and "${" in value
        ):
            continue
        if key and not (
            _NOT_A_CREDENTIAL.fullmatch(value)
            or _not_a_secret(
                value,
                None if _LEGACY_NAME.search(key) else key,
                group == "value",
                False,
                "",
            )
        ):
            results.append(Match("SECRET", *match.span(group), 0.97))
    for match in _userinfos(_URL_USERINFO, text, literal):
        if not _NOT_A_CREDENTIAL.fullmatch(text, *match.span("userinfo")):
            results.append(Match("SECRET", *match.span("userinfo"), 0.99))
    # A database URI's userinfo is DB_URI's, which can be turned off on its own.
    databases: set[int] = set()
    for match in _DB_URI.finditer(text):
        start, end = match.span(1)
        databases.add(start)
        query = text.find("?", start, end)
        if query < 0:
            continue
        for parameter in _QUERY_PARAMETER.finditer(text, query, end):
            if parameter["value"] and _SECRET_NAME.search(parameter["key"]):
                results.append(Match("SECRET", *parameter.span("value"), 0.97))
    for match in _userinfos(_URI_PASSWORD, text, literal):
        if not (
            match.start("userinfo") in databases
            or _NOT_A_CREDENTIAL.fullmatch(text, *match.span("password"))
            or _IMAGE_DIGEST.match(text, match.end())
        ):
            results.append(Match("SECRET", *match.span("userinfo"), 0.99))
    for match in _SPACED_ASSIGNMENT.finditer(text):
        quoted = match.group("quoted") is not None
        start, end = match.span("quoted" if quoted else "bare")
        before = text[match.start() - 1 : match.start()]
        attribute = match["closed"] or before in ("=", "<")
        if quoted:
            end = _quoted_end(literal, start, end)
        elif found := _VALUE_ATTRIBUTE_AT.match(text, start):
            quoted = bool(found["quote"])
            start, end = _attribute_value(literal, found)
            if end - start < 6:
                continue
        elif attribute and _ATTRIBUTE_NAME.match(text, start):
            continue
        if not _not_a_secret(text[start:end], None, quoted, False, ""):
            cut = end if quoted else _value_end(literal, start, end)
            if start < cut:
                results.append(Match("SECRET", start, cut, 0.97))
    for pattern in _ATTRIBUTE_PAIRS:
        for match in pattern.finditer(text):
            key = match["key"]
            start, end = _attribute_value(literal, match)
            value = text[start:end]
            new_key = None if _LEGACY_NAME.search(key) else key
            if len(value) >= 6 and not _not_a_secret(value, new_key, True, False, ""):
                results.append(Match("SECRET", start, end, 0.97))
    # A name read from a field is no gateway key: its value is read as any
    # key's is, so `value = var.db_password` is code.
    named = [
        (found["key"], True, *found.span("value"))
        for pattern in _ELEMENT_VALUES
        for found in pattern.finditer(text)
    ]
    for match in _PAIRED_VALUE.finditer(text):
        quoted = match["quoted"] is not None
        start, end = match.span("quoted" if quoted else "bare")
        if quoted:
            end = _quoted_end(literal, start, end)
        else:
            # An option's value is a shell word. A plain value runs to its
            # line's end, quotes and spaces too, but for the brackets it
            # closes: a password is never cut short where its end is unclear.
            found = (_UNQUOTED_END if match["option"] else _LINE_BREAK).search(
                literal, start
            )
            end = start + len(text[start : found.start() if found else end].rstrip())
            end = start + len(text[start : _value_end(literal, start, end)].rstrip())
        named.append((match["key"], quoted, start, end))
    for key, quoted, start, end in named:
        value = text[start:end]
        if len(value) >= 6 and not _not_a_secret(value, key, quoted, False, ""):
            results.append(Match("SECRET", start, end, 0.97))
    position = 0
    run = (0, 0)
    while match := _SECRET_ASSIGNMENT.search(text, position):
        quoted = match.group("quoted") is not None
        start, end = match.span("quoted" if quoted else "bare")
        if quoted:
            end = cut = _quoted_end(literal, start, end)
        else:
            if not run[0] <= start < run[1]:
                found = _BARE_END.search(literal, start)
                run = (start, found.start() if found else len(text))
            end = run[1]
            cut = _value_end(literal, start, end)
        position = start
        separator = text[match.end("tail") : match.start("quote") if quoted else start]
        annotation = match["type"]
        if annotation is not None and not (
            text[match.end("type")] in " \t"
            and (words := _NAME_RUN.findall(annotation))
            and all(map(_TYPE_WORD.fullmatch, words))
        ):
            # Not a type: it may be the password's start, so the mask runs
            # from it through the initializer.
            start, quoted = match.start("type"), True
            end = cut = max(cut, match.end())
        legacy = _LEGACY_NAME.search(text, match.start(), match.end("tail"))
        if legacy is None and "\n" in separator:
            continue
        new_key = None if legacy else text[match.start() : match.end("tail")]
        key = _KEY_NAME_START.search(text, max(0, match.start() - 64), match.start())
        key_start = key.start() if key else match.start()
        if not quoted and (
            # `${API_TOKEN:?message}`: the shell's check that it is set, and
            # `${APP_TOKEN:-${FALLBACK_TOKEN}}`: a default that is a variable.
            (
                text[key_start - 2 : key_start] == "${"
                and (text[start] == "?" or _DEFAULT_REFERENCE.match(text, start))
            )
            or _handed_on(text, key_start, start, end, separator)
        ):
            continue
        value = text[start : min(end, start + _MAX_SHAPED_VALUE + 1)]
        after = "" if quoted else text[end : end + 1]
        if start < cut and not _not_a_secret(
            value, new_key, quoted, ":" in separator, after
        ):
            results.append(Match("SECRET", start, cut, 0.97))
    return deduplicate(results)


def _next_jwt(text: str, position: int) -> re.Match[str] | None:
    """`_JWT.search`, which tried every `eyJ` in a run of token characters to
    the run's end: quadratic on `eyJeyJ...`. Each of them reads on to the same
    place, so when one fails the rest of its run does too."""
    while (start := text.find("eyJ", position)) >= 0:
        found = _JWT.match(text, start)
        if found:
            return found
        run_end = _NOT_TOKEN.search(text, start)
        position = run_end.start() if run_end else len(text)
    return None


def _vendor_tokens(text: str) -> Iterator[tuple[int, int]]:
    """What `finditer` found when `_JWT` was one more alternative of
    `_VENDOR_TOKEN`: the leftmost match of either, the search going on from its
    end. No vendor token starts with the `e` of a JWT, so they never tie."""
    token, jwt = _VENDOR_TOKEN.search(text), _next_jwt(text, 0)
    while token or jwt:
        first = min((each for each in (token, jwt) if each), key=re.Match.start)
        yield first.span()
        if token and token.start() < first.end():
            token = _VENDOR_TOKEN.search(text, first.end())
        if jwt and jwt.start() < first.end():
            jwt = _next_jwt(text, first.end())


def _scan_db_uri(text: str, as_written: bool = False) -> list[Match]:
    results: list[Match] = []
    for match in _DB_URI.finditer(text):
        start, end = match.span(1)
        at = text.rfind("@", start, end)
        if at > start:
            if not _NOT_A_CREDENTIAL.fullmatch(_as_decoded(text[start:at], as_written)):
                results.append(Match("DB_URI", start, at, 0.99))
            continue
        authority = _AUTHORITY_END.split(text[start:end], maxsplit=1)[0]
        host, colon, port = authority.rpartition(":")
        if colon and host and not host.startswith("[") and not port.isdigit():
            results.append(Match("DB_URI", start, start + len(authority), 0.99))
    return results


_BASE64_LINE = re.compile(
    r"(?:(?<![\w+/\\])|(?<=\\[nrt]))[A-Za-z0-9+/]{24,}={0,2}(?![\w+/=])"
)
_BASE64_RUN = r"[ \t]*([A-Za-z0-9+/]{4,}={0,2})[ \t]*(?=[\r\n\"']|\\[rn]|$)"
_BASE64_NEXT = re.compile(
    r"[ \t]*(?:\r\n?|\n|(?:\\r)?\\n)"
    r"(?:(?:[^\r\n:\\\"]+[:-])?\d+[:-]|[ \t]*\d+\t|-)?" + _BASE64_RUN
)
_DIFF_NEXT = re.compile(r"[ \t]*(?:\r\n?|\n)\+" + _BASE64_RUN)
_WRAP_WIDTHS = (60, 64, 76)
_BASIC_AUTH = re.compile(
    r'"auth"\s*:\s*"(?P<value>[A-Za-z0-9+/]{8,}={0,2})"'
    r"|(?<![\w-])(?:npm_config_)?_auth\s*=\s*(?P<quote>[\"']?)"
    r"(?P<bare>[A-Za-z0-9+/]{8,}={0,2})(?P=quote)(?![\w+/=])",
    re.IGNORECASE,
)


def _decoded(block: str) -> str | None:
    body = block.rstrip("=")
    if len(body) % 4 == 1:
        body = body[:-1]
    try:
        data = base64.b64decode(body + "=" * (-len(body) % 4), validate=True)
    except binascii.Error:
        return None
    return data.decode("utf-8", "replace")


def _holds_secret(text: str) -> bool:
    return bool(_scan_secret(text) or _scan_db_uri(text) or _scan_basic_auth(text))


def _encoded_block(
    text: str, start: int, end: int, follow: re.Pattern
) -> tuple[int, bool]:
    width = end - start
    parts, ends = [text[start:end]], [end]
    while (
        width in _WRAP_WIDTHS
        and len(parts[-1]) == width
        and not parts[-1].endswith("=")
    ):
        more = follow.match(text, ends[-1])
        if more is None or len(more.group(1)) > width:
            break
        parts.append(more.group(1))
        ends.append(more.end(1))
    for count in (len(parts), len(parts) - 1):
        decoded = _decoded("".join(parts[:count])) if count else None
        if decoded is not None:
            return ends[count - 1], _holds_secret(decoded)
    return end, False


def _scan_encoded_secret(text: str) -> list[Match]:
    results: list[Match] = []
    position = 0
    while found := _BASE64_LINE.search(text, position):
        start, end = found.span()
        indent = start
        while indent and text[indent - 1] in " \t":
            indent -= 1
        attempts = [(start, _BASE64_NEXT)]
        if text[indent - 1 : indent] == "+" and text[indent - 2 : indent - 1] in "\r\n":
            attempts.insert(0, (start, _DIFF_NEXT))
        elif text[start] == "+" and text[start - 1 : start] in "\r\n":
            attempts.insert(0, (start + 1, _DIFF_NEXT))
        if start == 0:
            attempts.extend((shift, _BASE64_NEXT) for shift in (1, 2, 3))
        position = end
        for begin, follow in attempts:
            stop, secret = _encoded_block(text, begin, end, follow)
            position = max(position, stop)
            if secret:
                results.append(Match("SECRET", begin, stop, 0.97))
                break
    return results


def _a_login(value: str) -> bool:
    body = value.rstrip("=")
    try:
        data = base64.b64decode(body + "=" * (-len(body) % 4), validate=True)
        login = data.decode("utf-8").rstrip("\r\n")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, colon, password = login.partition(":")
    return bool(colon and user and password and login.isprintable())


def _scan_basic_auth(text: str) -> list[Match]:
    results: list[Match] = []
    for found in _BASIC_AUTH.finditer(text):
        group = "value" if found.group("value") else "bare"
        if _a_login(found.group(group)):
            results.append(Match("SECRET", found.start(group), found.end(group), 0.97))
    return results


_WRITTEN_AT = re.compile(r"\[ *at *\]|\( *at *\)", re.IGNORECASE)
_LOCAL_PART = re.compile(rf"\w[{_LOCAL}.]{{0,63}}\Z")
_WRITTEN_DOMAIN = re.compile(r"[\w-]+(?:\.[\w-]+)+\b")


def _scan_written_at(text: str) -> list[Match]:
    """jane[at]example.com and jane(at)example.com, the whole written form.

    Anchored on each `[at]`, so the local part is read once, backwards and
    bounded, rather than from every word start.
    """
    results: list[Match] = []
    for at in _WRITTEN_AT.finditer(text):
        local = _LOCAL_PART.search(text, max(0, at.start() - 64), at.start())
        domain = _WRITTEN_DOMAIN.match(text, at.end())
        if local and domain and _validate_email("@" + domain.group()):
            results.append(Match("EMAIL_ADDRESS", local.start(), domain.end(), 1.0))
    return results


def _scan_addresses(text: str) -> list[Match]:
    """What the ported pattern matched, found from each `@`: the address starts
    at the first word boundary of the run before it, and not inside the one
    before."""
    results: list[Match] = []
    end = 0
    for run in _LOCAL_RUN.finditer(text):
        at = run.end()
        start = _LOCAL_START.search(text, max(run.start(), end), at)
        domain = _DOMAIN.match(text, at + 1)
        if start is None or text[at - 1] == "." or domain is None:
            continue
        end = domain.end()
        remote = _GIT_REMOTE.match(text, end)
        if remote and remote[0].endswith(".git"):
            continue
        if _validate_email(text[start.start() : end]):
            results.append(Match("EMAIL_ADDRESS", start.start(), end, 1.0))
    return results


def _scan_email(text: str, literal: str | None = None) -> list[Match]:
    if "@" not in text:
        return _scan_written_at(text)
    found = _scan_addresses(text)
    if "://" not in text:
        return found + _scan_written_at(text)
    literal = text if literal is None else literal
    authority = {
        match.end("userinfo")
        for pattern in (_URL_USERINFO, _URI_PASSWORD)
        for match in _userinfos(pattern, text, literal)
    }
    authority.update(
        text.rfind("@", *match.span(1)) for match in _DB_URI.finditer(text)
    )
    return [
        item for item in found if text.find("@", item.start, item.end) not in authority
    ] + _scan_written_at(text)


_PLATE_LETTER = "[A-PR-VYZa-pr-vyz]"
# The gateway's shape: one separator repeated between the groups, and not a
# longer number, a date, a version or a quantity. Case-sensitive for the units.
_PLATE_PATTERN = re.compile(
    r"(?<![\w-])(?:0[1-9]|[1-7][0-9]|8[01])(?P<separator>[ -]?)"
    rf"(?:{_PLATE_LETTER}(?P=separator)[0-9]{{4,5}}|"
    rf"{_PLATE_LETTER}{{2}}(?P=separator)[0-9]{{3,4}}|"
    rf"{_PLATE_LETTER}{{3}}(?P=separator)[0-9]{{2,3}})"
    r"(?!\w|[-./:,]\d)"
    r"(?!\s*(?:W|V|A|mA|mAh|kW|Hz|kHz|MHz|GHz|KB|GB|MB|TB|rpm)\b)",
    re.DOTALL | re.MULTILINE,
)
_NOT_PLATE_LETTERS = frozenset(
    "GB MB KB TB GHZ MHZ KM KG CM MM ML LT AM PM "
    "USD EUR TRY TL GBP CHF JPY CNY CAD AUD RUB SEK NOK DKK PLN AED SAR "
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC "
    "OCA SUB NIS HAZ TEM AGU EYL EKI KAS ARA".split()
)
# Never a plate in logs and code unless a plate word says so; the gateway does
# not list these.
_CODE_LETTERS = frozenset("GET PUT ERR CPU PID ID RC OK".split())
_PLATE_CUE = re.compile(r"\b(?:plaka|plate)\w*\W{0,3}\Z", re.IGNORECASE)


def _scan_plate(text: str) -> list[Match]:
    """Lowercase counts only after a plate word, so "15 dk 30" stays prose."""
    results: list[Match] = []
    for found in _PLATE_PATTERN.finditer(text):
        start, end = found.span()
        value = found.group()
        letters = "".join(filter(str.isalpha, value)).upper()
        if letters in _NOT_PLATE_LETTERS:
            continue
        if _PLATE_CUE.search(text, max(0, start - 24), start) or (
            value.isupper() and letters not in _CODE_LETTERS
        ):
            results.append(Match("TR_LICENSE_PLATE", start, end, 1.0))
    return results


_RECOGNIZERS: tuple[tuple[str, Callable[[str], list[Match]]], ...] = (
    ("PHONE_NUMBER", _scan_phone),
    (
        "CREDIT_CARD",
        lambda text: _scan(
            text, "CREDIT_CARD", _CARD_PATTERNS, validate=_validate_card
        ),
    ),
    ("IBAN_CODE", _scan_iban),
    ("IP_ADDRESS", _scan_ip),
    (
        "MAC_ADDRESS",
        lambda text: _scan(
            text, "MAC_ADDRESS", _MAC_PATTERNS, invalidate=_invalidate_mac
        ),
    ),
    ("US_SSN", _scan_ssn),
    (
        "TR_NATIONAL_ID",
        lambda text: _scan(
            text, "TR_NATIONAL_ID", _TCKN_PATTERNS, validate=_validate_tckn
        ),
    ),
    ("TR_VKN", _scan_vkn),
    ("TR_LICENSE_PLATE", _scan_plate),
    ("SECRET", _scan_encoded_secret),
    ("SECRET", _scan_basic_auth),
)


def scan_custom(text: str, patterns: tuple) -> list[Match]:
    results: list[Match] = []
    for pattern in patterns:
        for match in pattern.regex.finditer(text):
            start, end = match.span()
            if end > start:
                results.append(Match("CUSTOM", start, end, pattern.score, pattern.name))
    return deduplicate(results)


def analyze_text(
    text: str,
    entities: tuple[str, ...],
    custom: tuple = (),
    literal: str | None = None,
    as_written: bool = False,
) -> list[Match]:
    requested = frozenset(entities)
    results: list[Match] = []
    if "EMAIL_ADDRESS" in requested:
        results.extend(_scan_email(text, literal))
    for entity, scan in _RECOGNIZERS:
        if entity in requested:
            results.extend(scan(text))
    if "DB_URI" in requested:
        results.extend(_scan_db_uri(text, as_written))
    if "SECRET" in requested:
        results.extend(_scan_secret(text, literal))
    if custom and "CUSTOM" in requested:
        results.extend(scan_custom(text, custom))
    ibans = [
        (item.start, item.end) for item in results if item.entity_type == "IBAN_CODE"
    ]
    if ibans:
        # A card number inside a valid IBAN is that IBAN's middle digits.
        results = [
            item
            for item in results
            if item.entity_type != "CREDIT_CARD"
            or not any(start <= item.start and item.end <= end for start, end in ibans)
        ]
    return results
