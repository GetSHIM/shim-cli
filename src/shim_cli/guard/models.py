from __future__ import annotations

import math
from dataclasses import dataclass

from .entities import ENTITY_TYPES

_ENTITY_TYPES = frozenset(ENTITY_TYPES)


@dataclass(frozen=True)
class Finding:
    __slots__ = ("end", "entity_type", "score", "start")

    entity_type: str
    start: int
    end: int
    score: float

    def __post_init__(self) -> None:
        if self.entity_type not in _ENTITY_TYPES:
            raise ValueError("Unsupported Guard finding type.")
        if (
            isinstance(self.start, bool)
            or isinstance(self.end, bool)
            or not isinstance(self.start, int)
            or not isinstance(self.end, int)
            or self.start < 0
            or self.end <= self.start
        ):
            raise ValueError("Invalid Guard finding span.")
        if (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not math.isfinite(self.score)
            or not 0 <= self.score <= 1
        ):
            raise ValueError("Invalid Guard finding score.")
        object.__setattr__(self, "score", float(self.score))


@dataclass(frozen=True)
class GuardDecision:
    __slots__ = ("findings", "redacted_text")

    findings: tuple[Finding, ...]
    redacted_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.findings, tuple) or not all(
            isinstance(item, Finding) for item in self.findings
        ):
            raise ValueError("Invalid Guard findings.")
        if not isinstance(self.redacted_text, str):
            raise ValueError("Invalid Guard redacted text.")

    @property
    def blocked(self) -> bool:
        return bool(self.findings)

    @property
    def counts(self) -> tuple[tuple[str, int], ...]:
        counts: dict[str, int] = {}
        for finding in sorted(
            self.findings, key=lambda item: (item.start, item.end, item.entity_type)
        ):
            counts[finding.entity_type] = counts.get(finding.entity_type, 0) + 1
        return tuple(counts.items())
