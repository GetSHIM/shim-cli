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


def test_at_files_are_called_out_as_invisible_to_hooks() -> None:
    text = report.render(
        _session(_exchange(at_files=measure.AtFiles(count=3, bytes=8_120))), 62.0
    )

    assert "3 inlined" in text
    assert "invisible to hooks" in text


def test_spend_is_priced_per_kind_of_token() -> None:
    dollars, priced, unpriced = report.spend([_exchange()])

    assert priced == 1
    assert unpriced == []
    expected = (2 * 3.0 + 214 * 15.0 + 18_093 * 3.75 + 91_562 * 0.3) / 1_000_000
    assert abs(dollars - expected) < 1e-9


def test_an_unknown_model_is_named_rather_than_guessed() -> None:
    dollars, priced, unpriced = report.spend([_exchange(model="some-future-model")])

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
    text = report.render(_session(_exchange(entities={"SECRET": 2, "EMAIL": 1})), 9.0)

    assert "2 SECRET" in text
    assert "1 EMAIL" in text


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
