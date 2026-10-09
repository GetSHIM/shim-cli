from __future__ import annotations

import json

import pytest

from shim_cli.watch import measure, proxy, report


def _exchange(**changes):
    values = {
        "path": "/v1/messages",
        "model": "claude-sonnet-5",
        "status": 200,
        "request_bytes": 194_236,
        "usage": measure.Usage(
            input_tokens=2,
            output_tokens=214,
            cache_creation_input_tokens=18_093,
            cache_read_input_tokens=91_562,
        ),
        "sections": {
            "tools": 139_648,
            "system": 28_823,
            "messages": 25_552,
            "other": 213,
        },
    }
    values.update(changes)
    return measure.Exchange(**values)


def _session(*exchanges, errors: int = 0):
    session = proxy.Session()
    for exchange in exchanges:
        session.record(exchange)
    session.errors = errors
    return session


def test_nothing_seen_says_nothing() -> None:
    assert report.render(_session(), 12.0) == ""


def test_provider_numbers_are_marked_exact_and_shim_s_are_not() -> None:
    text = report.render(_session(_exchange()), 62.0)

    assert "(exact)" in text
    assert "approximate" in text
    for line in text.splitlines():
        if "(exact)" in line:
            assert "~" not in line


def test_the_cache_split_is_shown_because_it_dominates_the_bill() -> None:
    text = report.render(_session(_exchange()), 62.0)

    assert "109,657 tokens  (exact)" in text
    assert "cache read" in text
    assert "91,562" in text


def test_the_section_split_totals_the_exact_input() -> None:
    session = _session(_exchange(), _exchange())

    document = report.as_json(session, 30.0)

    assert (
        sum(document["approximate"]["tokens_by_section"].values())
        == (document["exact"]["input_tokens"])
    )


def test_the_tools_array_is_reported_as_the_largest_contributor() -> None:
    text = report.render(_session(_exchange()), 62.0)

    assert "tools" in text
    assert "largest" in text


def test_at_files_are_called_out_as_not_masked() -> None:
    text = report.render(
        _session(_exchange(at_files=measure.AtFiles(count=3, bytes=8_120))), 62.0
    )

    assert "3 inlined" in text
    assert "(not masked by any hook)" in text


def test_spend_is_priced_per_kind_of_token() -> None:
    dollars, priced, unpriced, costs = report.spend([_exchange()])

    assert priced == 1
    assert unpriced == []
    expected = (2 * 2.0 + 214 * 10.0 + 18_093 * 2.5 + 91_562 * 0.2) / 1_000_000
    assert abs(dollars - expected) < 1e-9
    assert costs == [dollars]


def test_an_unknown_model_is_named_rather_than_guessed() -> None:
    dollars, priced, unpriced, _ = report.spend([_exchange(model="some-future-model")])

    assert dollars == 0.0
    assert priced == 0
    assert unpriced == ["some-future-model"]

    text = report.render(_session(_exchange(model="some-future-model")), 5.0)
    assert "not priced for some-future-model" in text
    assert "$" not in text


@pytest.mark.parametrize(
    "model", ("claude\nforged-report", "x" * (measure.MAX_MODEL_CHARS + 1))
)
def test_invalid_model_labels_reach_neither_report(model: str) -> None:
    exchange = measure.inspect_request(json.dumps({"model": model}).encode())
    exchange.path = "/v1/messages"
    session = _session(exchange)

    assert model not in report.render(session, 5.0)
    assert model not in json.dumps(report.as_json(session, 5.0))
    assert report.as_json(session, 5.0)["approximate"]["unpriced_models"] == [
        measure.UNKNOWN_MODEL
    ]


def test_findings_in_traffic_are_counted_by_type() -> None:
    text = report.render(
        _session(
            _exchange(entities_by_section={"messages": {"SECRET": 2, "EMAIL": 1}})
        ),
        9.0,
    )

    assert "2 SECRET" in text
    assert "1 EMAIL" in text


def test_the_prompt_and_the_scaffolding_are_reported_apart() -> None:
    text = report.render(
        _session(
            _exchange(
                entities_by_section={"messages": {"SECRET": 2}, "tools": {"EMAIL": 1}}
            )
        ),
        9.0,
    )

    assert "request   2 SECRET in messages" in text
    assert "also      1 EMAIL in tool definitions" in text


def test_a_finding_only_outside_the_prompt_still_reads_as_found() -> None:
    text = report.render(
        _session(_exchange(entities_by_section={"system": {"EMAIL": 1}})), 9.0
    )

    assert "request   1 EMAIL in system prompt" in text
    assert "also" not in text


def test_every_section_that_contributed_is_named() -> None:
    text = report.render(
        _session(
            _exchange(
                entities_by_section={
                    "messages": {"EMAIL": 1},
                    "system": {"EMAIL": 1},
                    "tools": {"EMAIL": 2},
                    "other": {"EMAIL": 1},
                }
            )
        ),
        9.0,
    )

    assert (
        "also      4 EMAIL in system prompt, tool definitions and other fields" in text
    )


def test_the_json_report_splits_findings_by_section() -> None:
    document = report.as_json(
        _session(
            _exchange(
                entities_by_section={"messages": {"SECRET": 1}, "tools": {"SECRET": 2}}
            )
        ),
        5.0,
    )

    assert document["entities"] == {"SECRET": 3}
    assert document["entities_by_section"] == {
        "messages": {"SECRET": 1},
        "tools": {"SECRET": 2},
    }


def test_forwarding_failures_are_reported() -> None:
    text = report.render(_session(_exchange(), errors=2), 9.0)

    assert "2 request(s) could not be forwarded" in text


def test_only_model_requests_are_counted() -> None:
    session = _session(_exchange(), _exchange(path="/v1/organizations/me"))

    assert report.as_json(session, 5.0)["requests"] == 1


def test_the_json_report_separates_exact_from_approximate() -> None:
    document = report.as_json(_session(_exchange()), 62.0)

    assert set(document["exact"]) == {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "uncached_input_tokens",
    }
    assert "tokens_by_section" in document["approximate"]
    assert document["approximate"]["priced_on"] == report.PRICED_ON


def test_the_report_contains_no_traffic() -> None:
    document = report.as_json(_session(_exchange(entities={"SECRET": 1})), 5.0)

    assert "AKIA" not in json.dumps(document)
    assert set(document["entities"]) == {"SECRET"}


def test_the_report_says_nothing_was_modified() -> None:
    text = report.render(_session(_exchange()), 5.0)

    assert "nothing was modified" in text
    assert "no request body was written to disk" in text


def test_a_truncated_response_is_named_with_its_reason() -> None:
    session = _session(
        _exchange(stop_reason="max_tokens"),
        _exchange(stop_reason="end_turn"),
        _exchange(stop_reason="max_tokens"),
    )

    text = report.render(session, 9.0)

    assert "cut off   2 of 3 responses stopped at the output limit (max_tokens)" in text


def test_a_session_that_was_never_cut_off_says_nothing() -> None:
    text = report.render(_session(_exchange(stop_reason="end_turn")), 9.0)

    assert "cut off" not in text


def test_every_stop_reason_reaches_the_json_totals() -> None:
    session = _session(
        _exchange(stop_reason="max_tokens"),
        _exchange(stop_reason="end_turn"),
        _exchange(),
    )

    document = report.as_json(session, 5.0)

    assert document["stop_reasons"] == {"max_tokens": 1, "end_turn": 1}


def _both_ways(**changes):
    values = {
        "entities_by_section": {"messages": {"IBAN": 60, "EMAIL": 3}},
        "response_entities": {"text": {"EMAIL": 2}, "thinking": {"IBAN": 1}},
        "response_scan_status": "known",
    }
    values.update(changes)
    return _exchange(**values)


def test_the_two_directions_are_reported_apart_and_compared() -> None:
    text = report.render(_session(_both_ways()), 9.0)

    assert "request   60 IBAN, 3 EMAIL in messages" in text
    assert "response  2 EMAIL in model text; 1 IBAN in thinking" in text
    assert "IBAN   60 in request, 1 in response (thinking)" in text
    assert "EMAIL  3 in request, 2 in response" in text


def test_the_report_never_calls_a_model_s_own_words_a_leak() -> None:
    text = report.render(_session(_both_ways()), 9.0)

    assert report.NOT_LEAKS in text
    for word in ("leaked", "exposed", "was sent", "leak of"):
        assert word not in text


def test_a_clean_response_says_so_rather_than_saying_nothing() -> None:
    text = report.render(_session(_both_ways(response_entities={})), 9.0)

    assert "response  nothing found in model text or thinking" in text
    assert report.NOT_LEAKS not in text


def test_an_unscanned_response_is_counted_rather_than_assumed_clean() -> None:
    session = _session(
        _both_ways(),
        _both_ways(response_scan_status="unavailable", response_entities={}),
    )

    text = report.render(session, 9.0)

    assert "response scan unavailable or partial for 1 request(s)" in text


def test_a_response_only_finding_still_appears_in_the_comparison() -> None:
    exchange = _exchange(
        entities_by_section={},
        response_entities={"text": {"EMAIL": 2}},
        response_scan_status="known",
    )

    text = report.render(_session(exchange), 9.0)

    assert "EMAIL  0 in request, 2 in response" in text


def test_the_json_report_carries_both_directions_and_each_request() -> None:
    document = report.as_json(_session(_both_ways()), 5.0)

    assert document["response_entities"] == {
        "text": {"EMAIL": 2},
        "thinking": {"IBAN": 1},
    }
    assert document["response_scan"] == "known"
    assert document["exchanges"] == [
        {
            "entities_by_section": {"messages": {"IBAN": 60, "EMAIL": 3}},
            "response_entities": {"text": {"EMAIL": 2}, "thinking": {"IBAN": 1}},
            "response_scan_status": "known",
            "response_scan_reason": "",
            "stop_reason": "",
            "model": "claude-sonnet-5",
            "incomplete_reason": "",
            "auth_route": "",
            "request_bytes": 194_236,
            "usage_status": "unavailable",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 214,
                "cache_read_input_tokens": 91_562,
                "cache_creation_input_tokens": 18_093,
                "cache_creation_1h_input_tokens": 0,
            },
            "spend_usd": 0.065689,
            "priced_as": "claude-sonnet-5",
        }
    ]


def test_the_incomplete_line_says_why() -> None:
    """`inspection incomplete for 2 request(s)` with no cause left the person
    with nothing to act on — and the usual cause, parallel subagents, is one
    they can recognise if it is named."""
    busy = _exchange(measured=False, incomplete_reason=measure.SLOTS_BUSY)
    big = _exchange(measured=False, incomplete_reason=measure.BODY_TOO_LARGE)
    text = report.render(_session(busy, _exchange(), big), 90.0)

    line = next(row for row in text.splitlines() if "inspection incomplete" in row)
    assert "2 request(s):" in line
    assert "1 arrived while both inspection slots were busy" in line
    assert "1 had a body over the scan limit" in line


def test_a_measured_request_carries_no_reason() -> None:
    text = report.render(_session(_exchange()), 90.0)

    assert "inspection incomplete" not in text


def _spend_line(*exchanges) -> str:
    text = report.render(_session(*exchanges), 9.0)
    return next(row for row in text.splitlines() if row.startswith("  spend"))


def test_a_subscription_session_says_the_spend_is_not_a_bill() -> None:
    line = _spend_line(_exchange(auth_route="subscription"))

    assert line.endswith(
        f"(approximate, {report.PRICED_ON} prices; API-key equivalent, "
        "this session is on a subscription, not a bill)"
    )
    assert "$" in line


def test_an_api_key_session_keeps_the_spend_line_as_it_was() -> None:
    line = _spend_line(_exchange(auth_route="api-key"), _exchange(auth_route="api-key"))

    assert line.endswith(f"(approximate, {report.PRICED_ON} prices)")


@pytest.mark.parametrize(
    ("routes", "basis"),
    (
        (("subscription",) * 3 + ("api-key",), "mixed"),
        (("subscription",) * 3 + ("",), "unknown"),
    ),
)
def test_a_mixed_or_unknown_session_counts_the_subscription_requests(
    routes, basis
) -> None:
    exchanges = [_exchange(auth_route=route) for route in routes]
    unpriced = _exchange(model="some-future-model", auth_route="subscription")

    assert _spend_line(*exchanges, unpriced).endswith(
        "prices; 3 of 4 requests on a subscription)"
    )
    assert report.as_json(_session(*exchanges, unpriced), 5.0)["spend_basis"] == basis


@pytest.mark.parametrize(
    ("routes", "basis"),
    (
        (("api-key",), "api-key"),
        (("subscription", "subscription"), "subscription"),
        (("api-key", "subscription"), "mixed"),
        (("",), "unknown"),
        ((), "unknown"),
    ),
)
def test_the_json_names_the_spend_basis(routes, basis) -> None:
    document = report.as_json(
        _session(*(_exchange(auth_route=route) for route in routes)), 5.0
    )

    assert document["spend_basis"] == basis
    assert [row["auth_route"] for row in document["exchanges"]] == list(routes)


def _four_parallel():
    measured = [_exchange(response_scan_status="known") for _ in range(2)]
    skipped = [
        _exchange(
            measured=False,
            incomplete_reason=measure.SLOTS_BUSY,
            sections={},
            response_scan_status="unavailable",
        )
        for _ in range(2)
    ]
    return _session(*measured, *skipped)


def test_the_section_header_says_how_many_requests_it_covers() -> None:
    text = report.render(_four_parallel(), 9.0)

    assert (
        "  where the input went  (approximate — split by byte share; "
        "2 of 4 requests measured)"
    ) in text.splitlines()


def test_a_fully_measured_session_keeps_the_section_header() -> None:
    text = report.render(_session(_exchange(), _exchange()), 9.0)

    assert (
        "  where the input went  (approximate — split by byte share)"
        in text.splitlines()
    )


def test_an_unmeasured_request_has_one_reason_on_every_line() -> None:
    text = report.render(_four_parallel(), 9.0)
    busy = "2 arrived while both inspection slots were busy (parallel agents do this)"

    rows = text.splitlines()
    assert f"  inspection incomplete for 2 request(s): {busy}" in rows
    assert f"  response scan unavailable or partial for 2 request(s): {busy}" in rows
    assert "not streamed in a shape shim can read" not in text


def test_a_measured_unreadable_stream_still_says_so() -> None:
    session = _four_parallel()
    session.record(_exchange(response_scan_status="unavailable"))

    text = report.render(session, 9.0)

    line = next(row for row in text.splitlines() if "response scan" in row)
    assert "2 arrived while both inspection slots were busy" in line
    assert "1 not streamed in a shape shim can read" in line


def test_the_json_carries_the_response_scan_reason() -> None:
    session = _four_parallel()
    session.record(_exchange(response_scan_status="partial"))

    reasons = [
        row["response_scan_reason"] for row in report.as_json(session, 5.0)["exchanges"]
    ]

    assert reasons == ["", "", measure.SLOTS_BUSY, measure.SLOTS_BUSY, "partial"]


def test_the_response_line_does_not_claim_the_values_were_invented() -> None:
    assert report.NOT_LEAKS == (
        "written by the model; it may repeat values it was given"
    )


def test_each_exchange_carries_its_own_cost_and_the_row_it_was_priced_as() -> None:
    priced = _exchange(model="claude-opus-5-5-20260101")
    unpriced = _exchange(model="some-future-model")

    rows = report.as_json(_session(priced, unpriced), 5.0)["exchanges"]

    assert rows[0]["priced_as"] == "claude-opus-5-5"
    assert rows[0]["spend_usd"] == round(report.exchange_spend(priced), 6)
    assert rows[1]["priced_as"] is rows[1]["spend_usd"] is None


def test_the_costliest_request_is_named_once_two_are_priced() -> None:
    cheap = _exchange(model="claude-haiku-4-5")
    dear = _exchange(model="claude-opus-5-5")

    alone = report.render(_session(dear), 5.0)
    both = report.render(_session(cheap, dear), 5.0)

    assert "costliest" not in alone
    line = next(row for row in both.splitlines() if "costliest" in row)
    cost = report.exchange_spend(dear)
    assert line == (
        f"  costliest  one request ~${cost:,.2f} (claude-opus-5-5, "
        "109,657 input tokens)"
    )


def test_one_hour_cache_writes_are_priced_at_the_one_hour_rate() -> None:
    usage = measure.Usage(
        cache_creation_input_tokens=1_000_000, cache_creation_1h_input_tokens=400_000
    )

    cost = report.exchange_spend(_exchange(model="claude-opus-5-5", usage=usage))

    assert cost == pytest.approx(600_000 * 5 / 1e6 + 400_000 * 8 / 1e6)


@pytest.mark.parametrize(
    ("prompt", "rate"), [(99_999, 0.1), (100_000, 0.1), (100_001, 0.5)]
)
def test_haiku_5_5_prices_a_long_prompt_at_the_upper_row(prompt, rate) -> None:
    usage = measure.Usage(
        input_tokens=prompt - 2,
        cache_read_input_tokens=1,
        cache_creation_input_tokens=1,
    )

    cost = report.exchange_spend(_exchange(model="claude-haiku-5-5", usage=usage))

    read, write = (0.01, 0.125) if rate == 0.1 else (0.05, 0.625)
    assert cost == pytest.approx(((prompt - 2) * rate + read + write) / 1e6)


@pytest.mark.parametrize(("days", "stale"), [(90, False), (91, True)])
def test_the_spend_line_says_when_its_prices_are_old(monkeypatch, days, stale) -> None:
    import datetime

    read_on = datetime.date.fromisoformat(report.PRICED_ON)
    monkeypatch.setattr(report, "_today", lambda: read_on + datetime.timedelta(days))

    line = _spend_line(_exchange(auth_route="api-key"))
    document = report.as_json(_session(_exchange()), 5.0)

    old = (
        f"(approximate, {report.PRICED_ON} prices, older than 90 days; newer "
        "models and price changes are not reflected)"
    )
    assert line.endswith(old) is stale
    assert document["approximate"]["prices_stale"] is stale
