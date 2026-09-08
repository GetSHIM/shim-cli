from __future__ import annotations

import json
from pathlib import Path

import pytest

from shim_cli.guard import PLACEHOLDER, evaluate
from shim_cli.guard.entities import normalize_reveal

CORPUS = json.loads(
    (Path(__file__).resolve().parents[1] / "corpus" / "reveal-v1.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda case: case["id"])
def test_partial_reveal_output_is_exact(case: dict) -> None:
    reveal = normalize_reveal(case["reveal"])

    decision = evaluate(case["text"], reveal=reveal)

    assert decision.redacted_text == case["expected_output"]
    assert decision.redacted_text == PLACEHOLDER.sub(
        lambda match: match.group(), decision.redacted_text
    )


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda case: case["id"])
def test_revealing_changes_no_count_and_no_span(case: dict) -> None:
    plain = evaluate(case["text"])
    revealed = evaluate(case["text"], reveal=normalize_reveal(case["reveal"]))

    assert plain.counts == revealed.counts
    assert [(f.entity_type, f.start, f.end) for f in plain.findings] == [
        (f.entity_type, f.start, f.end) for f in revealed.findings
    ]
