from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from shim_cli.guard import evaluate

CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "parity-v1.json"
GENERATOR = Path(__file__).resolve().parents[2] / "scripts" / "build_parity_corpus.py"


def _generator() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "shim_parity_generator", GENERATOR
    )
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_DOCUMENT = json.loads(CORPUS.read_text(encoding="utf-8"))
_CASES = _DOCUMENT["cases"]

DELIBERATE_DIVERGENCES = {
    "net-3": (
        "0.0.0.0 is the unspecified address: it names no host and no person. "
        "Masking it stops the model telling 'bind to every interface' apart "
        "from 'loopback only' in a config file.",
        [],
    ),
    "net-10": (
        "::1 is loopback: the machine the code is already running on. "
        "Same reasoning as net-3.",
        [],
    ),
}


def test_every_divergence_is_still_a_real_case() -> None:
    assert set(DELIBERATE_DIVERGENCES) <= {case["id"] for case in _CASES}


def test_divergences_only_ever_relax_detection() -> None:
    by_id = {case["id"]: case for case in _CASES}
    for identifier, (_reason, expected) in DELIBERATE_DIVERGENCES.items():
        case = by_id[identifier]
        assert len(expected) < len(case["findings"]), identifier


def test_the_frozen_corpus_is_substantial() -> None:
    assert _DOCUMENT["case_count"] == len(_CASES) >= 400
    assert sum(len(case["findings"]) for case in _CASES) >= 300
    assert len({case["id"] for case in _CASES}) == len(_CASES)


def test_the_generator_still_produces_the_frozen_inputs() -> None:
    generated = _generator().cases()
    assert [(identifier, text) for identifier, text in generated] == [
        (case["id"], case["text"]) for case in _CASES
    ]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
def test_detection_is_unchanged(case: dict) -> None:
    decision = evaluate(case["text"])
    actual = [
        [finding.entity_type, finding.start, finding.end, finding.score]
        for finding in decision.findings
    ]
    if case["id"] in DELIBERATE_DIVERGENCES:
        reason, expected = DELIBERATE_DIVERGENCES[case["id"]]
        assert actual == expected, f"{case['id']} diverges on purpose: {reason}"
        assert decision.redacted_text == case["text"]
        return
    assert actual == case["findings"], (
        f"findings changed for {case['id']!r}\n"
        f"  text     : {case['text'][:120]!r}\n"
        f"  expected : {case['findings']}\n"
        f"  actual   : {actual}"
    )
    assert decision.redacted_text == case["redacted"], (
        f"redaction changed for {case['id']!r}\n"
        f"  expected : {case['redacted'][:200]!r}\n"
        f"  actual   : {decision.redacted_text[:200]!r}"
    )
