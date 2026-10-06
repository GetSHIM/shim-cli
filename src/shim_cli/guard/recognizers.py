"""Presidio 2.2.364 patterns (MIT, Microsoft)."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
import string
from typing import TYPE_CHECKING, NamedTuple

from .entities import PLACEHOLDER
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
_PHONE_CUE = re.compile(
    r"(?<![a-z])(?:telefon|tel|phone|gsm|cep|mobile|mobil|fax|whatsapp|call|numara"
    r"|num|no)(?:\W{1,3}(?:number|numaras[ıi]))?\W{0,4}\Z",
    re.IGNORECASE,
)
_PHONE_CUE_WINDOW = 24


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
_EMAIL_PATTERNS = _compile(
    (
        (
            r"\b((([!#$%&'*+\-/?^_`{|}~\w])|([!#$%&'*+\-/?^_`{|}~\w]"
            r"[!#$%&'*+\-/?^_`{|}~\.\w]{0,}[!#$%&'*+\-/?^_`{|}~\w]))"
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

    candidates = deduplicate(
        [
            Match("PHONE_NUMBER", match.start, match.end, _PHONE_SCORE)
            for region in _PHONE_REGIONS
            for match in phonenumbers.PhoneNumberMatcher(
                text, region, leniency=_PHONE_LENIENCY
            )
        ]
    )
    results: list[Match] = []
    for candidate in candidates:
        start, end = candidate.start, candidate.end
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
        if _BARE_DIGITS.fullmatch(raw) and not (
            _TURKISH_SHAPES.fullmatch(raw)
            or _PHONE_CUE.search(text, max(0, start - _PHONE_CUE_WINDOW), start)
        ):
            candidate = candidate._replace(entity_type=BARE_NUMBER)
        results.append(candidate)
    return results


_LEGACY_KEY = (
    r"password|passwd|pwd|api[_-]?key|secret|token|db[_-]?pass|postgres_password"
)
_SECRET_KEY = (
    _LEGACY_KEY
    + r"|access[_-]?key|private[_-]?key|signing[_-]?key|encryption[_-]?key"
    + r"|credentials?|auth[_-]?token|pass"
)
_SECRET_WORD = (
    r"(?:(?<![^\W_])|(?-i:(?<=[a-z0-9])(?=[A-Z])))"
    r"(?:" + _SECRET_KEY + r")"
    r"(?:(?![^\W_])|(?-i:(?<=[a-z])(?=[A-Z])))"
)
_SECRET_NAME = re.compile(_SECRET_WORD, re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    _SECRET_WORD + r"(?P<tail>[\w.-]{0,64})[\"']?\s*[=:]\s*"
    r"(?:(?P<quote>[\"'])(?P<quoted>[^\r\n]{6,}?)(?P=quote)"
    r"|(?P<bare>[^\s,}\]\"']{6}))",
    re.IGNORECASE,
)
_BARE_END = re.compile(r"[\s,}\]\"']")
_LEGACY_ASSIGNMENT = re.compile(
    r"(?<![\w-])[\"']?(?:" + _LEGACY_KEY + r")[\"']?\s*[=:]\s*\Z",
    re.IGNORECASE,
)
_REFERENCE = re.compile(
    r"\$\{\w+\}?|\$[A-Za-z_]\w*|%\w+%"
    r"|(?:os\.environ\W|process\.env\.|(?:os\.)?getenv\(|env\().*"
)
_TYPE_NAME = re.compile(
    r"(?:string|number|boolean|object|unknown|undefined|SecretStr|Optional\[\w*)"
    r"[;,?|)>\[\]]*"
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
_TYPE_VALUE = re.compile(r"(?P<name>(?:[A-Z][a-z]+){2,}|String)(?P<end>[?;)>]*)")
_NAME_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")
_MAX_SHAPED_VALUE = 1_024
_PATH_VALUE = re.compile(
    r"(?:~|\.{1,2})/\S*|/[a-z0-9._/-]+|/\S*\.[A-Za-z][A-Za-z0-9]{0,4}"
)
_URL_VALUE = re.compile(r"https?://", re.IGNORECASE)
_URL_USERINFO = re.compile(r"(?i)\b(?:https?|ftp|wss?)://(?P<userinfo>[^\s/?#'\"<>]+)@")
_DB_URI = re.compile(
    r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis(?:s)?|mssql)://"
    r"([^\s'\"<>]*)"
)
_NOT_A_CREDENTIAL = re.compile(r"[….*•:]+|(?:\$\{\w+\}|\{\w*\}|\$\w+|%\w+%|:)+")
_QUERY_PARAMETER = re.compile(r"[?&;](?P<key>[^=&;#]*)=(?P<value>[^&;#]+)")
_AUTHORITY_END = re.compile(r"[/?#]")
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
            r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
            r"(?<![\w-])(?:xox[abposr]-[0-9A-Za-z-]{10,}|AIza[0-9A-Za-z_-]{35}|"
            r"github_pat_[0-9A-Za-z_]{22,}|glpat-[0-9A-Za-z_-]{20,}|"
            r"npm_[0-9A-Za-z]{36}|hf_[0-9A-Za-z]{30,})(?![\w-]))"
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
            r'"identitytoken"\s*:\s*"(?P<value>(?!\$\{|<)[^"\s]{8,})"',
            re.IGNORECASE,
        ),
        "value",
        0.97,
        False,
    ),
    (
        re.compile(
            r"(?<![\w-])AccountKey=(?P<value>[A-Za-z0-9+/]{64,}={0,2})",
            re.IGNORECASE,
        ),
        "value",
        0.97,
        False,
    ),
    (
        re.compile(
            r"(?<![\w-])[\"']?authorization[\"']?\s*:\s*[\"']?(?:basic|bearer)\s+"
            r"(?P<value>[A-Za-z0-9._~+/=-]{6,})",
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


def _words(name: str) -> set[str]:
    return {word.lower() for word in _NAME_WORD.findall(name)}


def _a_type_of(key: str, value: str, after: str) -> bool:
    found = _TYPE_VALUE.fullmatch(value)
    if found is None:
        return False
    if found["name"] == "String":
        return True
    ended = bool(found["end"]) or after == ","
    return ended and bool(_words(found["name"]) & _words(key))


def _not_a_secret(
    value: str, new_key: str | None, quoted: bool, typed: bool, after: str
) -> bool:
    shaped = len(value) <= _MAX_SHAPED_VALUE
    if shaped and (
        _REFERENCE.fullmatch(value)
        or PLACEHOLDER.fullmatch(value.rstrip("`*.;:!?)"))
        or _TYPE_NAME.fullmatch(value)
    ):
        return True
    if new_key is None:
        return False
    code = not quoted and (
        _CODE_VALUE.fullmatch(value)
        or (after in ('"', "'", ",", "]") and _OPEN_CODE_VALUE.fullmatch(value))
        or (typed and _a_type_of(new_key, value, after))
    )
    return bool(
        _QUALIFIER.search(new_key)
        or _URL_VALUE.match(value)
        or (
            shaped
            and (code or _BARE_DIGITS.fullmatch(value) or _PATH_VALUE.fullmatch(value))
        )
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
    for match in _URL_USERINFO.finditer(text):
        if not _NOT_A_CREDENTIAL.fullmatch(match.group("userinfo")):
            results.append(Match("SECRET", *match.span("userinfo"), 0.99))
    for match in _DB_URI.finditer(text):
        start, end = match.span(1)
        query = text.find("?", start, end)
        if query < 0:
            continue
        for parameter in _QUERY_PARAMETER.finditer(text, query, end):
            if _SECRET_NAME.search(parameter.group("key")):
                results.append(Match("SECRET", *parameter.span("value"), 0.97))
    position = 0
    run = (0, 0)
    while match := _SECRET_ASSIGNMENT.search(text, position):
        quoted = match.group("quoted") is not None
        start, end = match.span("quoted" if quoted else "bare")
        if not quoted:
            if not run[0] <= start < run[1]:
                found = _BARE_END.search(text, start)
                run = (start, found.start() if found else len(text))
            end = run[1]
        position = start
        separator = text[match.end("tail") : match.start("quote") if quoted else start]
        legacy = _LEGACY_ASSIGNMENT.search(
            text, match.start(), match.start("quote") if quoted else start
        )
        if legacy is None and "\n" in separator:
            continue
        new_key = None if legacy else text[match.start() : match.end("tail")]
        value = text[start : min(end, start + _MAX_SHAPED_VALUE + 1)]
        after = "" if quoted else text[end : end + 1]
        if not _not_a_secret(value, new_key, quoted, ":" in separator, after):
            results.append(Match("SECRET", start, end, 0.97))
    return deduplicate(results)


def _scan_db_uri(text: str) -> list[Match]:
    results: list[Match] = []
    for match in _DB_URI.finditer(text):
        start, end = match.span(1)
        at = text.rfind("@", start, end)
        if at > start:
            if not _NOT_A_CREDENTIAL.fullmatch(text, start, at):
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


def _scan_email(text: str) -> list[Match]:
    if "@" not in text:
        return []
    found = _scan(text, "EMAIL_ADDRESS", _EMAIL_PATTERNS, validate=_validate_email)
    if "://" not in text:
        return found
    authority = {match.end("userinfo") for match in _URL_USERINFO.finditer(text)}
    authority.update(
        text.rfind("@", *match.span(1)) for match in _DB_URI.finditer(text)
    )
    return [
        item for item in found if text.find("@", item.start, item.end) not in authority
    ]


_RECOGNIZERS: tuple[tuple[str, Callable[[str], list[Match]]], ...] = (
    ("EMAIL_ADDRESS", _scan_email),
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
    text: str, entities: tuple[str, ...], custom: tuple = ()
) -> list[Match]:
    requested = frozenset(entities)
    results: list[Match] = []
    for entity, scan in _RECOGNIZERS:
        if entity in requested:
            results.extend(scan(text))
    if custom and "CUSTOM" in requested:
        results.extend(scan_custom(text, custom))
    return results
