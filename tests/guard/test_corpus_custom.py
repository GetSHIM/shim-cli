from __future__ import annotations

import json
from pathlib import Path

import pytest

from shim_cli.guard import evaluate
from shim_cli.guard.entities import compile_custom

CORPUS = json.loads(
    (Path(__file__).resolve().parents[1] / "corpus" / "custom-v1.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda case: case["id"])
def test_custom_pattern_output_is_exact(case: dict) -> None:
    decision = evaluate(case["text"], custom=compile_custom(case["custom"]))

    assert decision.redacted_text == case["expected_output"]
    assert dict(decision.custom_counts) == case["expected_custom"]


def test_every_case_names_a_distinct_behaviour() -> None:
    identifiers = [case["id"] for case in CORPUS["cases"]]

    assert len(identifiers) == len(set(identifiers))
    assert any(case["expected_custom"] for case in CORPUS["cases"])
    assert any(not case["expected_custom"] for case in CORPUS["cases"])
