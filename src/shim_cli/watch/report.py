from __future__ import annotations

import datetime
import re

from . import measure
from .measure import OTHER, RESPONSE_KINDS, SECTIONS, TRUNCATED, Usage

SECTION_WORDS = {
    "system": "system prompt",
    "tools": "tool definitions",
    OTHER: "other fields",
}
BESIDES = ("system", "tools", OTHER)
KIND_WORDS = {"text": "model text", "thinking": "thinking"}
NOT_LEAKS = "written by the model; it may repeat values it was given"

# USD per million tokens: input, 5-minute cache write, 1-hour cache write,
# cache read, output. Read from the Claude pricing page on PRICED_ON; every id
# was confirmed there or by GET /v1/models. An id not listed is unpriced: a
# neighbouring row priced Opus 4.5 to 5.5 at Opus 4 rates, up to 7.5 times over.
_FABLE_5_1 = (10.0, 12.5, 20.0, 0.25, 50.0)
_FABLE_5 = (10.0, 12.5, 20.0, 1.0, 50.0)
_OPUS_4_5 = (5.0, 6.25, 10.0, 0.5, 25.0)
_OPUS_4 = (15.0, 18.75, 30.0, 1.5, 75.0)
_SONNET_4 = (3.0, 3.75, 6.0, 0.3, 15.0)
PRICES = {
    "claude-fable-5-1": _FABLE_5_1,
    "claude-mythos-5-1": _FABLE_5_1,
    "claude-fable-5": _FABLE_5,
    "claude-mythos-5": _FABLE_5,
    "claude-opus-5-5": (4.0, 5.0, 8.0, 0.2, 20.0),
    "claude-opus-5": _OPUS_4_5,
    "claude-opus-4-8": _OPUS_4_5,
    "claude-opus-4-7": _OPUS_4_5,
    "claude-opus-4-6": _OPUS_4_5,
    "claude-opus-4-5": _OPUS_4_5,
    "claude-opus-4-1": _OPUS_4,
    "claude-opus-4": _OPUS_4,
    "claude-sonnet-5-5": (2.0, 2.5, 4.0, 0.1, 10.0),
    "claude-sonnet-5": (2.0, 2.5, 4.0, 0.2, 10.0),
    "claude-sonnet-4-6": _SONNET_4,
    "claude-sonnet-4-5": _SONNET_4,
    "claude-sonnet-4": _SONNET_4,
    "claude-haiku-5-5": (0.1, 0.125, 0.2, 0.01, 0.5),
    "claude-haiku-4-5": (1.0, 1.25, 2.0, 0.1, 5.0),
    "claude-3-5-haiku": (0.8, 1.0, 1.6, 0.08, 4.0),
}
# A prompt (input, cache writes and cache reads together) above the threshold
# pays the second row on every component of that request.
LONG_PROMPT = {"claude-haiku-5-5": (100_000, (0.5, 0.625, 1.0, 0.05, 2.5))}
PRICED_ON = "2026-10-09"
STALE_AFTER_DAYS = 90
_PER = 1_000_000
_SNAPSHOT = re.compile(r"-\d{8}$")


def model_id(model: str) -> str:
    """The table's key: one trailing `-latest` and one snapshot date dropped."""
    return _SNAPSHOT.sub("", model.removesuffix("-latest"))


def _price(model: str, prompt_tokens: int = 0):
    key = model_id(model)
    threshold, long_rates = LONG_PROMPT.get(key, (0, None))
    if long_rates is not None and prompt_tokens > threshold:
        return long_rates
    return PRICES.get(key)


def _today() -> datetime.date:
    return datetime.date.today()


def prices_stale() -> bool:
    age = _today() - datetime.date.fromisoformat(PRICED_ON)
    return age.days > STALE_AFTER_DAYS


def exchange_spend(exchange) -> float | None:
    usage = exchange.usage
    rates = _price(exchange.model or "", usage.total_input)
    if rates is None:
        return None
    fresh, five_minutes, one_hour, read, output = rates
    hour_writes = usage.cache_creation_1h_input_tokens
    return (
        usage.input_tokens * fresh
        + (usage.cache_creation_input_tokens - hour_writes) * five_minutes
        + hour_writes * one_hour
        + usage.cache_read_input_tokens * read
        + usage.output_tokens * output
    ) / _PER


def spend(exchanges: list) -> tuple:
    """Session total, priced count, unpriced model names, and each exchange's cost."""
    costs = [exchange_spend(exchange) for exchange in exchanges]
    unpriced = {
        exchange.model
        for exchange, cost in zip(exchanges, costs, strict=True)
        if cost is None and exchange.model
    }
    priced = [cost for cost in costs if cost is not None]
    return sum(priced), len(priced), sorted(unpriced), costs


def spend_basis(exchanges: list) -> str:
    routes = {e.auth_route for e in exchanges if _price(e.model or "")}
    if routes in ({"api-key"}, {"subscription"}):
        return routes.pop()
    return "unknown" if not routes or "" in routes else "mixed"


def totals(exchanges: list) -> Usage:
    combined = Usage()
    for exchange in exchanges:
        usage = exchange.usage
        combined = Usage(
            input_tokens=combined.input_tokens + usage.input_tokens,
            output_tokens=combined.output_tokens + usage.output_tokens,
            cache_creation_input_tokens=(
                combined.cache_creation_input_tokens + usage.cache_creation_input_tokens
            ),
            cache_read_input_tokens=(
                combined.cache_read_input_tokens + usage.cache_read_input_tokens
            ),
            cache_creation_1h_input_tokens=(
                combined.cache_creation_1h_input_tokens
                + usage.cache_creation_1h_input_tokens
            ),
        )
    return combined


def section_totals(exchanges: list) -> dict:
    combined: dict = {}
    for exchange in exchanges:
        for name, tokens in exchange.tokens_by_section().items():
            combined[name] = combined.get(name, 0) + tokens
    return combined


def entity_totals(exchanges: list) -> dict:
    combined: dict = {}
    for exchange in exchanges:
        for entity, count in exchange.entities.items():
            combined[entity] = combined.get(entity, 0) + count
    return combined


def entity_section_totals(exchanges: list) -> dict:
    combined: dict = {}
    for exchange in exchanges:
        for name, counts in exchange.entities_by_section.items():
            section = combined.setdefault(name, {})
            for entity, count in counts.items():
                section[entity] = section.get(entity, 0) + count
    return combined


def _listed(counts: dict) -> str:
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return ", ".join(f"{count} {entity}" for entity, count in ordered)


def _compared(by_section: dict, by_kind: dict) -> list:
    """Every entity seen on either side, most frequent first."""
    request: dict = {}
    for counts in by_section.values():
        for entity, count in counts.items():
            request[entity] = request.get(entity, 0) + count
    response: dict = {}
    kinds: dict = {}
    for kind, counts in by_kind.items():
        for entity, count in counts.items():
            response[entity] = response.get(entity, 0) + count
            kinds.setdefault(entity, []).append(kind)
    if not request and not response:
        return []
    names = sorted(
        set(request) | set(response),
        key=lambda name: (-(request.get(name, 0) + response.get(name, 0)), name),
    )
    width = max(len(name) for name in names)
    rows = []
    for name in names:
        seen = [kind for kind in RESPONSE_KINDS if kind in kinds.get(name, ())]
        where = f" ({', '.join(seen)})" if seen and seen != ["text"] else ""
        rows.append(
            f"{name:<{width}}  {request.get(name, 0)} in request, "
            f"{response.get(name, 0)} in response{where}"
        )
    return rows


def _words(names: list) -> str:
    said = [SECTION_WORDS[name] for name in names]
    return " and ".join([", ".join(said[:-1]), said[-1]] if len(said) > 2 else said)


def response_totals(exchanges: list) -> dict:
    combined: dict = {}
    for exchange in exchanges:
        for kind, counts in exchange.response_entities.items():
            found = combined.setdefault(kind, {})
            for entity, count in counts.items():
                found[entity] = found.get(entity, 0) + count
    return combined


def custom_totals(exchanges: list) -> dict:
    combined: dict = {"request": {}, "response": {}}
    for exchange in exchanges:
        for where, source in (
            ("request", exchange.custom),
            ("response", exchange.response_custom),
        ):
            for name, count in source.items():
                combined[where][name] = combined[where].get(name, 0) + count
    return combined


def response_scan_reason(exchange) -> str:
    if exchange.response_scan_status == "known":
        return ""
    if not exchange.measured and exchange.incomplete_reason:
        return exchange.incomplete_reason
    return exchange.response_scan_status


def response_scan(exchanges: list) -> str:
    if exchanges and all(e.response_scan_status == "known" for e in exchanges):
        return "known"
    if any(e.response_scan_status != "unavailable" for e in exchanges):
        return "partial"
    return "unavailable"


def stop_reason_totals(exchanges: list) -> dict:
    combined: dict = {}
    for exchange in exchanges:
        if exchange.stop_reason:
            reason = exchange.stop_reason
            combined[reason] = combined.get(reason, 0) + 1
    return combined


def at_file_totals(exchanges: list) -> tuple:
    return (
        sum(exchange.at_files.count for exchange in exchanges),
        sum(exchange.at_files.bytes for exchange in exchanges),
    )


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def _thousands(value: int) -> str:
    return f"{value:,}"


def _order(names) -> list:
    known = [name for name in SECTIONS if name in names]
    rest = sorted(name for name in names if name not in SECTIONS and name != OTHER)
    return known + rest + ([OTHER] if OTHER in names else [])


# What each reason means to the person reading it, not to the code.
_INCOMPLETE_SENTENCES = {
    measure.SLOTS_BUSY: (
        "arrived while both inspection slots were busy (parallel agents do this)"
    ),
    measure.BODY_TOO_LARGE: "had a body over the scan limit",
    measure.NOT_JSON: "could not be read as JSON",
    measure.TOO_MANY_FIELDS: "had more text fields than shim scans at once",
}


_RESPONSE_SENTENCES = {
    "unavailable": "not streamed in a shape shim can read",
    "partial": "stopped before the stream finished",
}


def _why_incomplete(exchanges) -> str:
    """`inspection incomplete for 2 request(s)` with no cause is unactionable."""
    counts: dict[str, int] = {}
    for exchange in exchanges:
        if exchange.measured:
            continue
        reason = exchange.incomplete_reason
        if reason in _INCOMPLETE_SENTENCES:
            counts[reason] = counts.get(reason, 0) + 1
    if not counts:
        return ""
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return ": " + ", ".join(
        f"{count} {_INCOMPLETE_SENTENCES[reason]}" for reason, count in ordered
    )


def render(session, seconds: float) -> str:
    exchanges = [
        exchange
        for exchange in session.exchanges
        if exchange.path.endswith(("messages", "responses", "completions"))
    ]
    if not exchanges and not session.errors:
        return ""
    plural = "" if len(exchanges) == 1 else "s"
    lines = [f"shim watch — {_duration(seconds)}, {len(exchanges)} request{plural}"]

    incomplete = sum(not exchange.measured for exchange in exchanges)
    unknown_usage = sum(exchange.usage_status != "known" for exchange in exchanges)
    if incomplete:
        why = _why_incomplete(exchanges)
        lines.append(f"  inspection incomplete for {incomplete} request(s){why}")
    if unknown_usage:
        lines.append(f"  usage unavailable or partial for {unknown_usage} request(s)")
    combined = totals(exchanges)
    if combined.total_input:
        lines.append(f"  input     {_thousands(combined.total_input)} tokens  (exact)")
        cached = combined.cache_read_input_tokens
        if cached:
            share = round(100 * cached / combined.total_input)
            lines.append(f"    cache read   {_thousands(cached)}   {share}%")
        if combined.cache_creation_input_tokens:
            lines.append(
                f"    cache write  {_thousands(combined.cache_creation_input_tokens)}"
            )
    if combined.output_tokens:
        lines.append(
            f"  output    {_thousands(combined.output_tokens)} tokens  (exact)"
        )

    cut_off = [e for e in exchanges if e.stop_reason in TRUNCATED]
    if cut_off:
        named = ", ".join(sorted({exchange.stop_reason for exchange in cut_off}))
        lines.append(
            f"  cut off   {len(cut_off)} of {len(exchanges)} responses stopped "
            f"at the output limit ({named})"
        )

    by_section = section_totals(exchanges)
    if by_section:
        total = sum(by_section.values())
        coverage = (
            f"; {len(exchanges) - incomplete} of {len(exchanges)} requests measured"
            if incomplete
            else ""
        )
        lines.append(
            f"  where the input went  (approximate — split by byte share{coverage})"
        )
        for name in _order(by_section):
            tokens = by_section[name]
            share = round(100 * tokens / total)
            lines.append(f"    {name:<9} ~{_thousands(tokens):>12}  {share:>3}%")

    count, size = at_file_totals(exchanges)
    if count:
        lines.append(
            f"  @ files   {count} inlined, {_thousands(size)} bytes "
            f"(not masked by any hook)"
        )

    by_section = entity_section_totals(exchanges)
    in_messages = by_section.get("messages", {})
    besides = [name for name in BESIDES if by_section.get(name)]
    elsewhere: dict = {}
    for name in besides:
        for entity, count in by_section[name].items():
            elsewhere[entity] = elsewhere.get(entity, 0) + count
    if in_messages:
        lines.append(f"  request   {_listed(in_messages)} in messages")
        if elsewhere:
            lines.append(f"  also      {_listed(elsewhere)} in {_words(besides)}")
    elif elsewhere:
        lines.append(f"  request   {_listed(elsewhere)} in {_words(besides)}")

    by_kind = response_totals(exchanges)
    scanned = [e for e in exchanges if e.response_scan_status != "unavailable"]
    if scanned:
        said = "; ".join(
            f"{_listed(by_kind[kind])} in {KIND_WORDS[kind]}"
            for kind in RESPONSE_KINDS
            if by_kind.get(kind)
        )
        lines.append(f"  response  {said or 'nothing found in model text or thinking'}")
        if said:
            lines.append(f"            {NOT_LEAKS}")
    unscanned = len(exchanges) - sum(
        exchange.response_scan_status == "known" for exchange in exchanges
    )
    if unscanned:
        states: dict[str, int] = {}
        for exchange in exchanges:
            if exchange.response_scan_status == "known":
                continue
            reason = response_scan_reason(exchange)
            said = _INCOMPLETE_SENTENCES.get(reason) or _RESPONSE_SENTENCES.get(
                reason, reason
            )
            states[said] = states.get(said, 0) + 1
        why = ", ".join(
            f"{count} {said}"
            for said, count in sorted(states.items(), key=lambda p: (-p[1], p[0]))
        )
        lines.append(
            f"  response scan unavailable or partial for {unscanned} request(s)"
            + (f": {why}" if why else "")
        )
    for index, line in enumerate(_compared(by_section, by_kind)):
        lines.append(f"  compare   {line}" if not index else f"            {line}")

    dollars, priced, unpriced, costs = spend(exchanges)
    if priced:
        basis = spend_basis(exchanges)
        if basis == "subscription":
            on = "; API-key equivalent, this session is on a subscription, not a bill"
        elif basis == "api-key":
            on = ""
        else:
            subscribed = sum(
                e.auth_route == "subscription"
                for e in exchanges
                if _price(e.model or "")
            )
            on = f"; {subscribed} of {priced} requests on a subscription"
        stale = (
            f", older than {STALE_AFTER_DAYS} days; newer models and price changes "
            "are not reflected"
            if prices_stale()
            else ""
        )
        lines.append(
            f"  spend     ~${dollars:,.2f}  (approximate, {PRICED_ON} prices{stale}{on})"
        )
    if priced >= 2:
        cost, costliest = max(
            (
                (cost, exchange)
                for cost, exchange in zip(costs, exchanges, strict=True)
                if cost is not None
            ),
            key=lambda pair: pair[0],
        )
        lines.append(
            f"  costliest  one request ~${cost:,.2f} ({costliest.model}, "
            f"{_thousands(costliest.usage.total_input)} input tokens)"
        )
    if unpriced:
        lines.append(f"  spend     not priced for {', '.join(unpriced)}")

    biggest = max(exchanges, key=lambda e: e.request_bytes, default=None)
    if biggest is not None and biggest.request_bytes:
        largest = max(biggest.sections.items(), key=lambda p: p[1], default=("", 0))
        if largest[1]:
            lines.append(
                f"  largest   one request was {_thousands(biggest.request_bytes)} "
                f"bytes, {largest[0]} {round(100 * largest[1] / biggest.request_bytes)}% of it"
            )

    if session.errors:
        lines.append(f"  errors    {session.errors} request(s) could not be forwarded")
    lines.append("  nothing was modified, and no request body was written to disk")
    return "\n".join(lines)


def as_json(session, seconds: float) -> dict:
    exchanges = [
        exchange
        for exchange in session.exchanges
        if exchange.path.endswith(("messages", "responses", "completions"))
    ]
    combined = totals(exchanges)
    dollars, priced, unpriced, costs = spend(exchanges)
    count, size = at_file_totals(exchanges)
    return {
        "seconds": round(seconds, 1),
        "requests": len(exchanges),
        "errors": session.errors,
        "inspection_incomplete": sum(not exchange.measured for exchange in exchanges),
        "usage_status": (
            "known"
            if exchanges
            and all(exchange.usage_status == "known" for exchange in exchanges)
            else "partial"
            if any(exchange.usage_status != "unavailable" for exchange in exchanges)
            else "unavailable"
        ),
        "exact": {
            "input_tokens": combined.total_input,
            "output_tokens": combined.output_tokens,
            "cache_read_input_tokens": combined.cache_read_input_tokens,
            "cache_creation_input_tokens": combined.cache_creation_input_tokens,
            "uncached_input_tokens": combined.input_tokens,
        },
        "approximate": {
            "tokens_by_section": section_totals(exchanges),
            "spend_usd": round(dollars, 4) if priced else None,
            "priced_on": PRICED_ON if priced else None,
            "prices_stale": prices_stale(),
            "unpriced_models": unpriced,
        },
        "spend_basis": spend_basis(exchanges),
        "at_files": {"count": count, "bytes": size},
        "entities": entity_totals(exchanges),
        "entities_by_section": entity_section_totals(exchanges),
        "stop_reasons": stop_reason_totals(exchanges),
        "response_entities": response_totals(exchanges),
        "response_scan": response_scan(exchanges),
        "custom": custom_totals(exchanges),
        "exchanges": [
            {
                "entities_by_section": exchange.entities_by_section,
                "response_entities": exchange.response_entities,
                "response_scan_status": exchange.response_scan_status,
                "response_scan_reason": response_scan_reason(exchange),
                "stop_reason": exchange.stop_reason,
                # Per request, not just the session total: whether a change to
                # the transcript breaks the provider's cache prefix is visible
                # only in which requests read from it and which rewrite it.
                "model": exchange.model,
                "incomplete_reason": exchange.incomplete_reason,
                "auth_route": exchange.auth_route,
                "request_bytes": exchange.request_bytes,
                "usage_status": exchange.usage_status,
                "usage": {
                    "input_tokens": exchange.usage.input_tokens,
                    "output_tokens": exchange.usage.output_tokens,
                    "cache_read_input_tokens": exchange.usage.cache_read_input_tokens,
                    "cache_creation_input_tokens": (
                        exchange.usage.cache_creation_input_tokens
                    ),
                    "cache_creation_1h_input_tokens": (
                        exchange.usage.cache_creation_1h_input_tokens
                    ),
                },
                "spend_usd": None if cost is None else round(cost, 6),
                "priced_as": None if cost is None else model_id(exchange.model),
            }
            for exchange, cost in zip(exchanges, costs, strict=True)
        ],
    }


__all__ = [
    "PRICED_ON",
    "PRICES",
    "as_json",
    "at_file_totals",
    "custom_totals",
    "entity_section_totals",
    "entity_totals",
    "exchange_spend",
    "model_id",
    "prices_stale",
    "response_scan",
    "response_scan_reason",
    "response_totals",
    "render",
    "section_totals",
    "spend",
    "spend_basis",
    "stop_reason_totals",
    "totals",
]
