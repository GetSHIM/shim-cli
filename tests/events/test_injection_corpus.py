"""The Turkish injection cases, in the gateway's corpus shape so the gateway can
copy the file and flag the same text."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shim_cli.events import injection

CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "injection-tr-v1.json"
_DOCUMENT = json.loads(CORPUS.read_text(encoding="utf-8"))


def test_the_file_is_in_the_gateway_corpus_shape() -> None:
    assert _DOCUMENT["format"] == "shim.injection.corpus"
    assert _DOCUMENT["version"] == 1
    ids = [case["id"] for case in _DOCUMENT["cases"]]
    assert len(ids) == len(set(ids))
    expected = {marker for case in _DOCUMENT["cases"] for marker in case["expect"]}
    assert expected == set(injection.MARKERS) - {injection.HIDDEN_TEXT}


@pytest.mark.parametrize("case", _DOCUMENT["cases"], ids=lambda case: case["id"])
def test_each_case_flags_exactly_its_markers(case: dict) -> None:
    assert list(injection.scan(case["text"])) == case["expect"]
