from __future__ import annotations

import re
from collections.abc import Iterable

from .analyze import analyze
from .entities import ENTITY_TYPES, MAX_REVEAL_DIGITS
from .models import GuardDecision

# The one definition of what a placeholder looks like, in both forms.
PLACEHOLDER = re.compile(rf"<[A-Z_]+_[0-9]+(?::[0-9]{{1,{MAX_REVEAL_DIGITS}}})?>")


def _tail(span: str, digits: int) -> str:
    """The last few digits, ignoring the separators the value was printed with."""
    return "".join(character for character in span if character.isdigit())[-digits:]


def evaluate(
    text: str,
    enabled_entities: Iterable[str] = ENTITY_TYPES,
    custom: tuple = (),
    reveal: dict | None = None,
) -> GuardDecision:
    findings = analyze(text, enabled_entities, custom)
    if not findings:
        return GuardDecision((), text)

    counts: dict[str, int] = {}
    pieces: list[str] = []
    cursor = 0
    for finding in findings:
        counts[finding.entity_type] = counts.get(finding.entity_type, 0) + 1
        marker = f"{finding.entity_type}_{counts[finding.entity_type]}"
        digits = (reveal or {}).get(finding.entity_type, 0)
        tail = _tail(text[finding.start : finding.end], digits) if digits else ""
        pieces.append(text[cursor : finding.start])
        pieces.append(f"<{marker}:{tail}>" if tail else f"<{marker}>")
        cursor = finding.end
    pieces.append(text[cursor:])
    return GuardDecision(findings, "".join(pieces))
