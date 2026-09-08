from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

import pytest

from shim_cli.guard import BUILT_IN_TYPES, evaluate

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "probe"
PROMPTS = json.loads((CORPUS_DIR / "guard-v2.json").read_text(encoding="utf-8"))
METRICS = json.loads((CORPUS_DIR / "guard-v2-metrics.json").read_text(encoding="utf-8"))
TOOLS = json.loads((CORPUS_DIR / "guard-tools-v1.json").read_text(encoding="utf-8"))
INLINE_LIMIT = 2_000
REWRITABLE = {
    "user-prompt": False,
    "outbound": True,
    "inbound": True,
    "local-write": False,
    "executable-text": False,
}


def _diff(expected: str, actual: str) -> str:
    return (
        "\n".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                "expected",
                "actual",
                n=1,
                lineterm="",
            )
        )
        or f"expected {expected!r}\nactual   {actual!r}"
    )


def _at(payload: object, path: list) -> object:
    value = payload
    for key in path:
        value = value[key]  # type: ignore[index]
    return value


@pytest.mark.parametrize("case", PROMPTS["cases"], ids=lambda case: case["id"])
def test_prompt_corpus_output_is_exact(case: dict) -> None:
    decision = evaluate(case["text"])

    assert [f.entity_type for f in decision.findings] == case["categories"]
    assert decision.blocked is bool(case["categories"])
    assert decision.redacted_text == case["expected_output"], (
        f"redaction changed for {case['id']!r}:\n"
        + _diff(case["expected_output"], decision.redacted_text)
    )
    if "expected_spans" in case:
        actual = [[f.start, f.end, f.entity_type] for f in decision.findings]
        assert actual == case["expected_spans"], (
            f"source spans changed for {case['id']!r}\n"
            f"  expected {case['expected_spans']}\n"
            f"  actual   {actual}"
        )
        for start, end, _type in case["expected_spans"]:
            assert case["text"][start:end], "a span must cover source text"


def test_prompt_corpus_schema_is_complete() -> None:
    cases = PROMPTS["cases"]

    assert PROMPTS["version"] == 2
    assert PROMPTS["evaluation_unit"] == "exact redacted output"
    assert tuple(PROMPTS["categories"]) == BUILT_IN_TYPES
    assert len(cases) >= 45
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        assert "expected_output" in case, case["id"]
        needs_spans = not case["text"].isascii() or "%" in case["text"]
        assert ("expected_spans" in case) is needs_spans, case["id"]


def test_prompt_corpus_covers_every_category_and_the_assignment_rule() -> None:
    cases = PROMPTS["cases"]
    positive = {c for case in cases for c in case["categories"]}
    negative_ids = {case["id"] for case in cases if not case["categories"]}

    assert positive == set(BUILT_IN_TYPES)
    assert {
        f"{category.lower().replace('_', '-')}-safe-negative"
        for category in BUILT_IN_TYPES
    } <= negative_ids

    prose = [
        case
        for case in cases
        if not case["categories"]
        and any(
            word in case["text"].lower()
            for word in ("password", "token", "secret", "api key")
        )
    ]
    assert len(prose) >= 6, "the assignment rule needs at least six prose negatives"


def test_published_prompt_metrics_match_the_detector() -> None:
    cases = PROMPTS["cases"]
    predictions = {
        case["id"]: {f.entity_type for f in evaluate(case["text"]).findings}
        for case in cases
    }
    for category in BUILT_IN_TYPES:
        expected = {c["id"] for c in cases if category in c["categories"]}
        predicted = {i for i, cats in predictions.items() if category in cats}
        published = METRICS["categories"][category]

        assert published["true_positive"] == len(expected & predicted)
        assert published["false_positive"] == len(predicted - expected)
        assert published["false_negative"] == len(expected - predicted)
        assert published["negative_cases"] == len(cases) - len(expected)
        assert published["precision"] == published["recall"] == 1.0

    assert METRICS["evaluation_unit"] == "exact redacted output"
    assert METRICS["exact_output"] == {
        "cases": len(cases),
        "matching": len(cases),
        "ratio": 1.0,
    }
    spanned = [case for case in cases if "expected_spans" in case]
    assert METRICS["exact_source_spans"]["cases"] == len(spanned)
    assert METRICS["exact_source_spans"]["ratio"] == 1.0


@pytest.mark.parametrize("case", TOOLS["cases"], ids=lambda case: case["id"])
def test_tool_corpus_output_is_exact(case: dict) -> None:
    payload = json.loads((FIXTURES / case["fixture"]).read_text(encoding="utf-8"))
    assert payload["hook_event_name"] == case["event"]

    for scanned in case["scanned"]:
        value = _at(payload, scanned["path"])
        assert isinstance(value, str)
        assert len(value) == scanned["input_chars"]
        decision = evaluate(value)

        assert [f.entity_type for f in decision.findings] == scanned["categories"], (
            f"{case['id']} at {scanned['path']}"
        )
        emitted = value if not case["rewritable"] else decision.redacted_text
        if "expected_output" in scanned:
            assert emitted == scanned["expected_output"], (
                f"{case['id']} at {scanned['path']}:\n"
                + _diff(scanned["expected_output"], emitted)
            )
        else:
            digest = hashlib.sha256(emitted.encode()).hexdigest()
            assert digest == scanned["expected_output_sha256"], (
                f"{case['id']} at {scanned['path']}: "
                f"{len(emitted)} characters hashed to {digest}"
            )


def test_tool_corpus_never_rewrites_disk_or_command_payloads() -> None:
    protected = [
        case
        for case in TOOLS["cases"]
        if case["direction"] in ("local-write", "executable-text")
    ]
    assert protected, "the corpus must cover payloads that are never rewritten"

    for case in protected:
        assert case["rewritable"] is False, case["id"]
        payload = json.loads((FIXTURES / case["fixture"]).read_text(encoding="utf-8"))
        for scanned in case["scanned"]:
            value = _at(payload, scanned["path"])
            assert scanned["unchanged"] is True, case["id"]
            assert scanned["expected_output"] == value, case["id"]
            assert scanned["categories"], f"{case['id']} should still detect"


def test_tool_corpus_schema_and_coverage() -> None:
    cases = TOOLS["cases"]

    assert TOOLS["version"] == 1
    assert len(cases) >= 20
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["direction"] for case in cases} <= set(REWRITABLE)
    for case in cases:
        assert case["rewritable"] is REWRITABLE[case["direction"]], case["id"]
        assert (FIXTURES / case["fixture"]).is_file(), case["id"]
        assert case["scanned"], case["id"]
        for scanned in case["scanned"]:
            inline = "expected_output" in scanned
            assert inline is (
                scanned["input_chars"] <= INLINE_LIMIT or not case["rewritable"]
            ), case["id"]

    events = {case["event"] for case in cases}
    assert {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PostToolBatch",
    } <= events

    tools = {case.get("tool") for case in cases}
    assert {"Bash", "Read", "Write", "WebFetch"} <= tools
    assert any(name and name.startswith("mcp__") for name in tools)


def test_tool_corpus_covers_the_five_required_payload_shapes() -> None:
    required = {
        "claude-pretooluse-bash-connection-string",
        "claude-pretooluse-mcp-argument-email",
        "claude-posttooluse-read-dotenv",
        "claude-posttooluse-webfetch-summary",
        "claude-pretooluse-write-secret",
    }
    assert required <= {case["id"] for case in TOOLS["cases"]}
