from __future__ import annotations

import re

INSTRUCTION_OVERRIDE = "INSTRUCTION_OVERRIDE"
ROLE_REASSIGNMENT = "ROLE_REASSIGNMENT"
SYSTEM_IMPERSONATION = "SYSTEM_IMPERSONATION"
SECRECY_REQUEST = "SECRECY_REQUEST"
HIDDEN_TEXT = "HIDDEN_TEXT"

MARKERS = (
    INSTRUCTION_OVERRIDE,
    ROLE_REASSIGNMENT,
    SYSTEM_IMPERSONATION,
    SECRECY_REQUEST,
    HIDDEN_TEXT,
)

_INVISIBLE = re.compile("[\u200b\u2060-\u2064\u202a-\u202e\U000e0000-\U000e007f]")

# Turkish: the same patterns the gateway uses. Only imperatives end at a word
# boundary, so "kuralları yok saydık" (a report of the past) is not an order.
_TR_QUALIFIER = r"\b(?:önceki|yukarıdaki|tüm|bütün|eski|mevcut)\b"
_TR_RULE = r"\b(?:talimat|komut|kural|yönerge|direktif|yönlendirme)\w*"
_TR_IGNORE = (
    r"\b(?:yok say(?:ın|ınız)?|görmezden gel(?:in|iniz)?|unut(?:un|unuz)?"
    r"|dikkate alma(?:yın|yınız)?|uyma(?:yın|yınız)?|geçersiz say(?:ın|ınız)?"
    r"|iptal et(?:in|iniz)?)\b"
)

_PATTERNS = (
    (
        INSTRUCTION_OVERRIDE,
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}?"
            r"\b(?:previous|prior|above|earlier|all)\b[^.\n]{0,20}?"
            r"\b(?:instruction|prompt|rule|direction|command)s?\b",
            re.IGNORECASE,
        ),
    ),
    (
        INSTRUCTION_OVERRIDE,
        re.compile(
            rf"{_TR_QUALIFIER}[^.\n]{{0,40}}?{_TR_RULE}[^.\n]{{0,30}}?{_TR_IGNORE}"
            rf"|{_TR_IGNORE}[^.\n]{{0,30}}?{_TR_QUALIFIER}[^.\n]{{0,40}}?{_TR_RULE}",
            re.IGNORECASE,
        ),
    ),
    (
        ROLE_REASSIGNMENT,
        re.compile(
            r"\b(?:you\s{1,4}are\s{1,4}now|from\s{1,4}now\s{1,4}on\s{1,4}you"
            r"|act\s{1,4}as\s{1,4}(?:a|an|the)\b"
            r"|pretend\s{1,4}(?:to\s{1,4}be|you\s{1,4}are))",
            re.IGNORECASE,
        ),
    ),
    (
        ROLE_REASSIGNMENT,
        re.compile(
            r"\b(?:sen artık|artık sen|bundan sonra sen|bundan böyle sen)\b"
            # Imperatives only: "liste gibi davranır" describes, it does not order.
            r"|\b(?:gibi davran(?:ın|ınız)?|rolüne gir(?:in|iniz)?"
            r"|rolünü üstlen(?:in|iniz)?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        SYSTEM_IMPERSONATION,
        re.compile(
            r"(?:^|\n)[^\S\n]{0,8}(?:\[|<|\#{1,3}[^\S\n]{0,4})?"
            r"(?:system|assistant|sistem|asistan)[^\S\n]{0,4}(?:\]|>|:)"
            r"|<[^\S\n]{0,4}/?[^\S\n]{0,4}"
            r"(?:system|important_instructions)[^\S\n]{0,4}>",
            re.IGNORECASE,
        ),
    ),
    (
        SECRECY_REQUEST,
        re.compile(
            r"\b(?:do\s{0,4}not|don'?t|never)\b[^.\n]{0,30}?"
            r"\b(?:tell|inform|mention|reveal|show|disclose)\b[^.\n]{0,20}?"
            r"\b(?:the\s{1,4})?(?:user|human|operator)\b",
            re.IGNORECASE,
        ),
    ),
    (
        SECRECY_REQUEST,
        re.compile(
            r"\bkullanıcı(?:ya|dan)\b[^.\n]{0,30}?"
            r"\b(?:söyleme|bildirme|gösterme|açıklama|belirtme|bahsetme|gizle)",
            re.IGNORECASE,
        ),
    ),
)

MIN_TEXT_CHARACTERS = 24


def scan(text: str) -> tuple:
    if len(text) < MIN_TEXT_CHARACTERS:
        return ()
    found = []
    if _INVISIBLE.search(text):
        found.append(HIDDEN_TEXT)
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            found.append(name)
    return tuple(name for name in MARKERS if name in found)


__all__ = [
    "HIDDEN_TEXT",
    "INSTRUCTION_OVERRIDE",
    "MARKERS",
    "MIN_TEXT_CHARACTERS",
    "ROLE_REASSIGNMENT",
    "SECRECY_REQUEST",
    "SYSTEM_IMPERSONATION",
    "scan",
]
