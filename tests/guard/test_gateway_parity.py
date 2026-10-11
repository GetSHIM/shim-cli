"""shim-cli against the gateway's own detection corpus.

Every case runs with every type enabled. A case agrees when shim-cli's set of
(type, value) equals the gateway's `expect`, whatever `known_gap` says: `expect`
is the intended answer. Every other case is listed below with its reason and
shim-cli's exact output, so the two detectors cannot drift apart silently.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from shim_cli.guard import ENTITY_TYPES, evaluate

CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "gateway-detection-v1.json"
GATEWAY_COMMIT = "ca6b2e96158e3ba8dcd1ef90f21e831c1d7fc104"
GATEWAY_CORPUS_SHA256 = (
    "4b5a4fbb8418d2b657edf87751bf90c00f2ae5187b66ce468041a52e393242a9"
)
GATEWAY_NAMES = {"EMAIL_ADDRESS": "EMAIL", "PHONE_NUMBER": "PHONE", "IBAN_CODE": "IBAN"}

NO_TYPE = "shim-cli has no type for this (FILE_PATH, PERSON, LOCATION)."
PRECISION_FIRST = (
    "An uncued bare number the gateway calls a phone; in coding traffic shim-cli "
    "counts it as a bare number unless it is Turkish-shaped or cued."
)
CUE_VOCABULARY = "A phone cue word the gateway knows and shim-cli does not."
NOT_PORTED = "A gateway rule shim-cli has not ported; the entry names it."
REASONS = (NO_TYPE, PRECISION_FIRST, CUE_VOCABULARY, NOT_PORTED)

GATEWAY_DIVERGENCES = {
    # The gateway masks the whole URI; shim-cli masks its user-info only and
    # keeps scheme, host, port and database (PRD-36).
    "postgres-uri": (NOT_PORTED, [("DB_URI", "user:pw")]),
    "file-path-home": (NO_TYPE, []),
    # The gateway's run-of-more-than-four-numbers rule (`_in_decimal`).
    "not-ip-five-parts": (NOT_PORTED, [("IP_ADDRESS", "1.2.3.4")]),
    # A VKN with no tax word near it; shim-cli asks for the context word.
    "vkn-bare": (NOT_PORTED, []),
    "phone-after-an-unrelated-word": (PRECISION_FIRST, []),
    "phone-without-cue-us": (PRECISION_FIRST, []),
    "phone-without-cue-de-mobile": (PRECISION_FIRST, []),
    "phone-number-phrase": (PRECISION_FIRST, []),
    "phone-word-beats-code-word": (CUE_VOCABULARY, []),
    "phone-whatsapp-link-us": (PRECISION_FIRST, []),
    # Tier 3 dotted phones.
    "phone-tr-dotted-area-code": (NOT_PORTED, []),
    "phone-tr-dotted-after-word": (NOT_PORTED, []),
    "number-after-no-cue-is-masked": (PRECISION_FIRST, []),
    "person-name": (NO_TYPE, []),
    "street-address": (NO_TYPE, []),
}

_DOCUMENT = json.loads(CORPUS.read_text(encoding="utf-8"))


def _found(text: str) -> list[tuple[str, str]]:
    decision = evaluate(text, ENTITY_TYPES)
    return sorted((f.entity_type, text[f.start : f.end]) for f in decision.findings)


def test_the_corpus_is_the_gateway_s_own() -> None:
    digest = hashlib.sha256(CORPUS.read_bytes()).hexdigest()

    assert digest == GATEWAY_CORPUS_SHA256, (
        "copy the gateway corpus again and update both constants and the "
        f"divergence table (GATEWAY_COMMIT is {GATEWAY_COMMIT})"
    )
    assert _DOCUMENT["format"] == "shim.detection.corpus"
    assert _DOCUMENT["version"] == 1


def test_every_divergence_is_a_case_and_every_reason_is_used() -> None:
    assert set(GATEWAY_DIVERGENCES) <= {case["id"] for case in _DOCUMENT["cases"]}
    assert {reason for reason, _ in GATEWAY_DIVERGENCES.values()} == set(REASONS)


@pytest.mark.parametrize("case", _DOCUMENT["cases"], ids=lambda case: case["id"])
def test_shim_cli_finds_what_the_gateway_finds(case: dict) -> None:
    expect = sorted(
        (GATEWAY_NAMES.get(item["entity"], item["entity"]), item["value"])
        for item in case["expect"]
    )
    found = _found(case["text"])

    if case["id"] not in GATEWAY_DIVERGENCES:
        assert found == expect
        return
    reason, pinned = GATEWAY_DIVERGENCES[case["id"]]
    assert found != expect, f"{case['id']} now agrees with the gateway: remove it"
    assert found == sorted(pinned), f"{case['id']} diverges on purpose: {reason}"
