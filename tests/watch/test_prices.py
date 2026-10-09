"""The price table, pinned. Changing a rate without moving READ_ON, or the
reverse, fails here; so does pricing an id by its neighbour."""

from __future__ import annotations

import pytest

from shim_cli.watch import report

# platform.claude.com "Model pricing", read on this date. Each id was confirmed
# on the model pages or by GET /v1/models. claude-opus-4-0 and claude-sonnet-4-0
# are left out: no current page lists those aliases.
READ_ON = "2026-10-09"
# (ids, prompt tokens, input, 5-minute write, 1-hour write, cache read, output)
TABLE = (
    (("claude-fable-5-1", "claude-mythos-5-1"), 0, 10, 12.50, 20, 0.25, 50),
    (("claude-fable-5", "claude-mythos-5"), 0, 10, 12.50, 20, 1, 50),
    (("claude-opus-5-5",), 0, 4, 5, 8, 0.20, 20),
    (
        (
            "claude-opus-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-opus-4-5",
        ),
        0,
        5,
        6.25,
        10,
        0.50,
        25,
    ),
    (("claude-opus-4-1", "claude-opus-4"), 0, 15, 18.75, 30, 1.50, 75),
    (("claude-sonnet-5-5",), 0, 2, 2.50, 4, 0.10, 10),
    (("claude-sonnet-5",), 0, 2, 2.50, 4, 0.20, 10),
    (
        ("claude-sonnet-4-6", "claude-sonnet-4-5", "claude-sonnet-4"),
        0,
        3,
        3.75,
        6,
        0.30,
        15,
    ),
    (("claude-haiku-5-5",), 100_000, 0.10, 0.125, 0.20, 0.01, 0.50),
    (("claude-haiku-5-5",), 100_001, 0.50, 0.625, 1, 0.05, 2.50),
    (("claude-haiku-4-5",), 0, 1, 1.25, 2, 0.10, 5),
    (("claude-3-5-haiku",), 0, 0.80, 1, 1.60, 0.08, 4),
)
CASES = [
    (model, prompt, rates)
    for ids, prompt, *rates in TABLE
    for model in ids
    for model in (model, f"{model}-20260101", f"{model}-latest")
]


def test_the_table_was_read_on_the_date_the_report_prints() -> None:
    assert report.PRICED_ON == READ_ON


@pytest.mark.parametrize(("model", "prompt", "rates"), CASES)
def test_every_documented_id_is_priced_at_its_own_row(model, prompt, rates) -> None:
    assert report._price(model, prompt) == tuple(rates)


def test_every_priced_id_is_in_the_documented_table() -> None:
    documented = {model for ids, *_ in TABLE for model in ids}

    assert set(report.PRICES) == documented


@pytest.mark.parametrize(
    ("model", "dollars"),
    [
        ("claude-opus-4-5-20251101", 30),
        ("claude-opus-4-6", 30),
        ("claude-opus-4-7", 30),
        ("claude-opus-4-8", 30),
        ("claude-opus-5", 30),
        ("claude-opus-5-5", 24),
    ],
)
def test_a_million_in_and_out_on_opus_costs_the_list_price(model, dollars) -> None:
    from shim_cli.watch import measure

    exchange = measure.Exchange(
        path="/v1/messages",
        model=model,
        usage=measure.Usage(input_tokens=1_000_000, output_tokens=1_000_000),
    )

    assert report.exchange_spend(exchange) == pytest.approx(dollars)


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-9",
        "claude-sonnet-6",
        "claude-opus",
        "opus-4-5",
        "claude-opus-4-0",
        "claude-sonnet-4-0",
        "claude-opus-5-5-fast",
    ],
)
def test_an_id_not_in_the_table_is_never_priced_as_a_neighbour(model) -> None:
    assert report._price(model) is None
