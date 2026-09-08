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
        ROLE_REASSIGNMENT,
        re.compile(
            r"\b(?:you\s{1,4}are\s{1,4}now|from\s{1,4}now\s{1,4}on\s{1,4}you"
            r"|act\s{1,4}as\s{1,4}(?:a|an|the)\b"
            r"|pretend\s{1,4}(?:to\s{1,4}be|you\s{1,4}are))",
            re.IGNORECASE,
        ),
    ),
    (
        SYSTEM_IMPERSONATION,
        re.compile(
            r"(?:^|\n)[^\S\n]{0,8}(?:\[|<|\#{1,3}[^\S\n]{0,4})?"
            r"(?:system|assistant)[^\S\n]{0,4}(?:\]|>|:)"
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
