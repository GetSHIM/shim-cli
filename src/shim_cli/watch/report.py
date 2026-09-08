from __future__ import annotations

from .measure import OTHER, RESPONSE_KINDS, SECTIONS, TRUNCATED, Usage

SECTION_WORDS = {
    "system": "system prompt",
    "tools": "tool definitions",
    OTHER: "other fields",
}
BESIDES = ("system", "tools", OTHER)
KIND_WORDS = {"text": "model text", "thinking": "thinking"}
NOT_LEAKS = "model-generated content is counted here, not leaks"

PRICES = (
    ("claude-opus-4", (15.0, 75.0, 18.75, 1.5)),
    ("claude-opus-5", (15.0, 75.0, 18.75, 1.5)),
    ("claude-sonnet-4", (3.0, 15.0, 3.75, 0.3)),
    ("claude-sonnet-5", (3.0, 15.0, 3.75, 0.3)),
    ("claude-haiku-4", (1.0, 5.0, 1.25, 0.1)),
    ("claude-3-5-haiku", (0.8, 4.0, 1.0, 0.08)),
)
PRICED_ON = "2026-08-30"
_PER = 1_000_000


def _price(model: str):
    for prefix, rates in PRICES:
        if model.startswith(prefix):
            return rates
    return None


def spend(exchanges: list) -> tuple:
    total = 0.0
    priced = 0
    unpriced = set()
    for exchange in exchanges:
        rates = _price(exchange.model or "")
        if rates is None:
            if exchange.model:
                unpriced.add(exchange.model)
            continue
        fresh, output, write, read = rates
        usage = exchange.usage
        total += (
            usage.input_tokens * fresh
            + usage.output_tokens * output
            + usage.cache_creation_input_tokens * write
            + usage.cache_read_input_tokens * read
        ) / _PER
        priced += 1
    return total, priced, sorted(unpriced)


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


def render(session, seconds: float) -> str:
    exchanges = [
        exchange
        for exchange in session.exchanges
        if exchange.path.endswith(("messages", "responses", "completions"))
    ]
    if not exchanges and not session.errors:
        return ""
    lines = [f"shim watch — {_duration(seconds)}, {len(exchanges)} requests"]

    incomplete = sum(not exchange.measured for exchange in exchanges)
    unknown_usage = sum(exchange.usage_status != "known" for exchange in exchanges)
    if incomplete:
        lines.append(f"  inspection incomplete for {incomplete} request(s)")
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
        lines.append("  where the input went  (approximate — split by byte share)")
        for name in _order(by_section):
            tokens = by_section[name]
            share = round(100 * tokens / total)
            lines.append(f"    {name:<9} ~{_thousands(tokens):>12}  {share:>3}%")

    count, size = at_file_totals(exchanges)
    if count:
        lines.append(
            f"  @ files   {count} inlined, {_thousands(size)} bytes "
            f"(invisible to hooks)"
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
        lines.append(
            f"  response scan unavailable or partial for {unscanned} request(s)"
        )
    for index, line in enumerate(_compared(by_section, by_kind)):
        lines.append(f"  compare   {line}" if not index else f"            {line}")

    dollars, priced, unpriced = spend(exchanges)
    if priced:
        lines.append(f"  spend     ~${dollars:,.2f}  (approximate, {PRICED_ON} prices)")
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
    dollars, priced, unpriced = spend(exchanges)
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
            "unpriced_models": unpriced,
        },
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
                "stop_reason": exchange.stop_reason,
                # Per request, not just the session total: whether a change to
                # the transcript breaks the provider's cache prefix is visible
                # only in which requests read from it and which rewrite it.
                "model": exchange.model,
                "request_bytes": exchange.request_bytes,
                "usage_status": exchange.usage_status,
                "usage": {
                    "input_tokens": exchange.usage.input_tokens,
                    "output_tokens": exchange.usage.output_tokens,
                    "cache_read_input_tokens": exchange.usage.cache_read_input_tokens,
                    "cache_creation_input_tokens": (
                        exchange.usage.cache_creation_input_tokens
                    ),
                },
            }
            for exchange in exchanges
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
    "response_scan",
    "response_totals",
    "render",
    "section_totals",
    "spend",
    "stop_reason_totals",
    "totals",
]
