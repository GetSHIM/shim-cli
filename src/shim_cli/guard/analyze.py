from __future__ import annotations

import bisect
import contextlib
import re
import signal
import threading
from collections.abc import Iterable, Iterator

from .entities import (
    DEFAULT_ENTITIES,
    ENTITY_TYPES,
    MAX_REVEAL_DIGITS,
    normalize_entities,
)
from .models import Finding
from .normalize import normalize
from .recognizers import BARE_NUMBER, ENTITY_MAP, Match, analyze_text

ANALYSIS_DEADLINE_SECONDS = 20
_PRIORITY = {
    "DB_URI": 100,
    "SECRET": 90,
    "CREDIT_CARD": 80,
    "TR_NATIONAL_ID": 70,
    "TR_VKN": 70,
    "US_SSN": 70,
    "IBAN": 60,
    "MAC_ADDRESS": 50,
    "IP_ADDRESS": 40,
    "EMAIL": 30,
    "TR_LICENSE_PLATE": 25,
    "PHONE": 20,
    "CUSTOM": 10,
}


class _AnalysisTimeout(TimeoutError):
    pass


@contextlib.contextmanager
def _deadline() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous_delay, previous_interval = signal.getitimer(signal.ITIMER_REAL)
    outer = 0 < previous_delay <= ANALYSIS_DEADLINE_SECONDS
    delay = previous_delay if outer else ANALYSIS_DEADLINE_SECONDS

    def expire(_signal_number: int, _frame: object) -> None:
        if outer:
            raise TimeoutError("shim deadline exceeded")
        raise _AnalysisTimeout("shim analysis deadline exceeded")

    previous_handler = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, delay)
    try:
        yield
    finally:
        left = signal.setitimer(signal.ITIMER_REAL, 0)[0]
        signal.signal(signal.SIGALRM, previous_handler)
        remaining = previous_delay - delay + left
        if remaining > 0:
            signal.setitimer(signal.ITIMER_REAL, remaining, previous_interval)


def _validated(items: Iterable[Match], text_length: int) -> list[Finding]:
    unique: dict[tuple[str, int, int], Finding] = {}
    for item in items:
        entity_type = getattr(item, "entity_type", None)
        start = getattr(item, "start", None)
        end = getattr(item, "end", None)
        score = getattr(item, "score", None)
        label = getattr(item, "label", "")
        if not isinstance(entity_type, str) or entity_type not in ENTITY_MAP:
            raise ValueError("Guard analyzer returned an unsupported finding.")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or end > text_length
        ):
            raise ValueError("Guard analyzer returned an invalid span.")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("Guard analyzer returned an invalid score.")
        finding = Finding(ENTITY_MAP[entity_type], start, end, score, label)
        key = (finding.entity_type, start, end)
        previous = unique.get(key)
        if previous is None or finding.score > previous.score:
            unique[key] = finding
    return sorted(
        unique.values(), key=lambda item: (item.start, item.end, item.entity_type)
    )


def _resolve_overlaps(items: Iterable[Finding]) -> list[Finding]:
    ordered = sorted(items, key=lambda item: (item.start, item.end, item.entity_type))
    resolved: list[Finding] = []
    component: list[Finding] = []
    component_end = -1

    def collapse() -> None:
        if not component:
            return
        if len(component) == 1:
            resolved.append(component[0])
            return
        winner = min(
            component,
            key=lambda item: (
                -_PRIORITY[item.entity_type],
                -(item.end - item.start),
                item.entity_type,
                item.start,
                item.end,
                -item.score,
            ),
        )
        resolved.append(
            Finding(
                winner.entity_type,
                min(item.start for item in component),
                max(item.end for item in component),
                winner.score,
                winner.label,
                winner.value or (winner.start, winner.end),
            )
        )

    for item in ordered:
        if component and item.start >= component_end:
            collapse()
            component = []
        component.append(item)
        component_end = max(component_end, item.end)
    collapse()

    return resolved


def _source_findings(
    items: Iterable[Finding], source_spans: tuple[tuple[int, int], ...]
) -> list[Finding]:
    mapped: list[Finding] = []
    for item in items:
        spans = source_spans[item.start : item.end]
        if len(spans) != item.end - item.start or not spans:
            raise ValueError("Guard analyzer returned an incomplete span.")
        previous: tuple[int, int] | None = None
        for start, end in spans:
            if (
                start < 0
                or end <= start
                or (
                    previous is not None
                    and start < previous[1]
                    and (start, end) != previous
                )
            ):
                raise ValueError("Guard normalization returned an invalid span.")
            previous = (start, end)
        value = None
        if item.value is not None:
            value = (source_spans[item.value[0]][0], source_spans[item.value[1] - 1][1])
        mapped.append(
            Finding(
                item.entity_type,
                spans[0][0],
                spans[-1][1],
                item.score,
                item.label,
                value,
            )
        )
    return _resolve_overlaps(mapped)


# The placeholders shim writes, and no other: `<ADMIN_2024>` may hold a value.
# A number counts the values of one type in one event, and six digits are more
# than a 1 MB event holds.
_OWN_PLACEHOLDER = re.compile(
    rf"<(?:{'|'.join(ENTITY_TYPES)})_[0-9]{{1,6}}"
    rf"(?::[0-9]{{1,{MAX_REVEAL_DIGITS}}})?>"
)


def _not_placeholders(findings: list[Finding], text: str) -> list[Finding]:
    """`findings` without those that are only shim's own placeholders.

    Masked text is scanned again: by the Stop count, by `shim audit`, when the
    model reads back what it was given. `--password=<SECRET_1>` and a custom
    `[A-Z]+_[0-9]+` read a value in it, and a credential shim had masked was
    reported as reaching the model. A match with a letter or a digit outside
    every placeholder is kept whole: it may be a credential written against
    one.
    """
    blanked = _OWN_PLACEHOLDER.sub(lambda match: " " * len(match.group()), text)
    if blanked == text:
        return findings
    kept = []
    for finding in findings:
        outside = blanked[finding.start : finding.end]
        if outside == text[finding.start : finding.end] or any(
            character.isalnum() for character in outside
        ):
            kept.append(finding)
    return kept


def _unclaimed(bare: list[Match], findings: list[Finding]) -> int:
    starts = [finding.start for finding in findings]
    count = 0
    for item in bare:
        index = bisect.bisect_left(starts, item.end)
        if not index or findings[index - 1].end <= item.start:
            count += 1
    return count


def analyze(
    text: str,
    enabled_entities: Iterable[str] = DEFAULT_ENTITIES,
    custom: tuple = (),
) -> tuple[Finding, ...]:
    return analyze_counting(text, enabled_entities, custom)[0]


def analyze_counting(
    text: str,
    enabled_entities: Iterable[str] = DEFAULT_ENTITIES,
    custom: tuple = (),
) -> tuple[tuple[Finding, ...], int]:
    enabled = frozenset(normalize_entities(enabled_entities))
    if not enabled:
        return (), 0
    normalized = normalize(text)
    if not normalized.text:
        return (), 0
    source_entities = tuple(
        source for source, public in ENTITY_MAP.items() if public in enabled
    )
    try:
        with _deadline():
            raw = analyze_text(
                normalized.text, source_entities, custom, normalized.literal
            )
        bare = [item for item in raw if item.entity_type == BARE_NUMBER]
        claimed = [item for item in raw if item.entity_type != BARE_NUMBER]
        normalized_findings = _resolve_overlaps(
            _not_placeholders(
                _validated(claimed, len(normalized.text)), normalized.text
            )
        )
    except _AnalysisTimeout as error:
        raise ValueError("Guard analysis exceeded its runtime limit.") from error
    except (TimeoutError, ValueError):
        raise
    except Exception as error:
        raise ValueError("Guard analysis failed safely.") from error
    # The ASCII fast path hands the text back unchanged, every character its own.
    findings = (
        normalized_findings
        if normalized.text is text
        else _source_findings(normalized_findings, normalized.source_spans)
    )
    return tuple(findings), _unclaimed(bare, normalized_findings)
