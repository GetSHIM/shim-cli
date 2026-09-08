from __future__ import annotations

from collections.abc import Iterable

from .analyze import analyze
from .entities import ENTITY_TYPES
from .models import GuardDecision


def evaluate(
    text: str, enabled_entities: Iterable[str] = ENTITY_TYPES
) -> GuardDecision:
    findings = analyze(text, enabled_entities)
    if not findings:
        return GuardDecision((), text)

    counts: dict[str, int] = {}
    pieces: list[str] = []
    cursor = 0
    for finding in findings:
        counts[finding.entity_type] = counts.get(finding.entity_type, 0) + 1
        pieces.append(text[cursor : finding.start])
        pieces.append(f"<{finding.entity_type}_{counts[finding.entity_type]}>")
        cursor = finding.end
    pieces.append(text[cursor:])
    return GuardDecision(findings, "".join(pieces))
