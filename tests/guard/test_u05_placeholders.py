"""A placeholder names one value, keeps the lines it replaced, and is never
read as a value itself when masked text is scanned again."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from shim_cli.guard import BUILT_IN_TYPES, evaluate
from shim_cli.guard.entities import compile_custom

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "probe"
PROMPTS = json.loads((CORPUS_DIR / "guard-v2.json").read_text(encoding="utf-8"))
TOOLS = json.loads((CORPUS_DIR / "guard-tools-v1.json").read_text(encoding="utf-8"))
JANE = "jane.doe@example.com"
JOHN = "john.roe@example.com"
AWS_EXAMPLE = "AKIAIOSFODNN7EXAMPLE"


def test_one_value_written_twice_keeps_one_number() -> None:
    decision = evaluate(f"from {JANE} to {JOHN}, cc {JANE}")

    assert decision.redacted_text == "from <EMAIL_1> to <EMAIL_2>, cc <EMAIL_1>"
    # Every occurrence is still a finding: the count is of values found.
    assert decision.counts == (("EMAIL", 3),)


def test_a_shared_numbering_carries_from_one_call_to_the_next() -> None:
    numbering: dict = {}

    first = evaluate(f"owner {JANE}", numbering=numbering)
    second = evaluate(f"billing {JOHN}, owner {JANE}", numbering=numbering)

    assert first.redacted_text == "owner <EMAIL_1>"
    assert second.redacted_text == "billing <EMAIL_2>, owner <EMAIL_1>"


def test_the_same_value_decoded_from_a_url_keeps_its_number() -> None:
    decision = evaluate(f"mailto:jane.doe%40example.com and {JANE}")

    assert decision.redacted_text == "mailto:<EMAIL_1> and <EMAIL_1>"


PEM = (
    "KEY = '''\n-----BEGIN RSA PRIVATE KEY-----\n"
    + "\n".join(["0123456789abcdef" * 4] * 3)
    + "\n-----END RSA PRIVATE KEY-----\n'''\nraise ValueError\n"
)


def test_a_masked_block_keeps_every_line_it_spanned() -> None:
    decision = evaluate(PEM)

    assert decision.counts == (("SECRET", 1),)
    assert decision.redacted_text.count("\n") == PEM.count("\n")
    lines = decision.redacted_text.split("\n")
    assert lines.index("raise ValueError") == PEM.split("\n").index("raise ValueError")
    assert lines[1] == "<SECRET_1>"
    assert "0123456789abcdef" not in decision.redacted_text


@pytest.mark.parametrize("case", PROMPTS["cases"], ids=lambda case: case["id"])
def test_a_masked_prompt_scanned_again_finds_nothing(case: dict) -> None:
    assert evaluate(case["expected_output"], BUILT_IN_TYPES).findings == ()


def _at(document: object, path: list) -> object:
    for key in path:
        document = document[key]  # type: ignore[index]
    return document


@pytest.mark.parametrize(
    ("case", "scanned"),
    [(case, scanned) for case in TOOLS["cases"] for scanned in case["scanned"]],
    ids=lambda item: item["id"] if "id" in item else "/".join(map(str, item["path"])),
)
def test_a_masked_tool_field_scanned_again_finds_nothing(
    case: dict, scanned: dict
) -> None:
    payload = json.loads((FIXTURES / case["fixture"]).read_bytes())
    masked = evaluate(_at(payload, scanned["path"])).redacted_text

    assert evaluate(masked).findings == ()
    if "expected_output" in scanned and not scanned.get("unchanged"):
        assert evaluate(scanned["expected_output"]).findings == ()


@pytest.mark.parametrize(
    "text",
    [
        "mysql --password=<SECRET_1>",
        "mysql --password <SECRET_1>",
        'mysql --password "<SECRET_1>" -u app',
        "mysql --password='<SECRET_1>' -u app",
        "mysql --password=`<SECRET_1>`",
        "password=`<SECRET_1>`",
        "pass: <SECRET_1>>",
        "pay <IBAN_1:1326> or <CREDIT_CARD_2:4242>",
    ],
)
def test_shims_own_placeholder_is_never_a_finding(text: str) -> None:
    decision = evaluate(text)

    assert decision.findings == ()
    assert decision.redacted_text == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mysql --password=<ADMIN_2024>", "mysql --password=<SECRET_1>"),
        ("phone <USER_905320101234>", "phone <USER_<PHONE_1>>"),
        ("phone <PHONE_905320101234>", "phone <PHONE_<PHONE_1>>"),
    ],
)
def test_only_a_placeholder_shim_writes_is_let_go(text: str, expected: str) -> None:
    """`<ADMIN_2024>` is no type shim has, and no event holds that many values
    of one type: what is in them may be a value."""
    assert evaluate(text).redacted_text == expected


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "ACME", "pattern": "<ACME_[0-9]+>"},
        {"name": "ACME", "literal": "<ACME_42>"},
    ],
)
def test_a_custom_entry_shaped_like_a_placeholder_still_matches(entry: dict) -> None:
    decision = evaluate("see <ACME_42>", custom=compile_custom([entry]))

    assert decision.redacted_text == "see <CUSTOM_1>"
    assert decision.custom_counts == (("ACME", 1),)


def test_a_custom_pattern_does_not_match_inside_a_placeholder() -> None:
    custom = compile_custom([{"name": "TICKET", "pattern": "[A-Z]+_[0-9]+"}])

    decision = evaluate(f"owner <EMAIL_7> for OPS_12 and {JANE}", custom=custom)

    # Numbered above the <EMAIL_7> the text already holds (F081).
    assert decision.redacted_text == "owner <EMAIL_7> for <CUSTOM_1> and <EMAIL_8>"
    assert decision.custom_counts == (("TICKET", 1),)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Numbered above the <SECRET_7> the text holds (F081).
        (f"<SECRET_7>{AWS_EXAMPLE}", "<SECRET_7><SECRET_8>"),
        ('token = "<SECRET_7>0123456789abcdef"', 'token = "<SECRET_8>"'),
    ],
)
def test_a_credential_written_against_a_placeholder_is_still_masked(
    text: str, expected: str
) -> None:
    """Only a placeholder, with nothing but punctuation around it, is let go:
    a match holding anything else may be a credential, and is masked whole."""
    decision = evaluate(text)

    assert decision.redacted_text == expected
    assert decision.counts == (("SECRET", 1),)


@pytest.mark.parametrize(
    "written",
    [
        "TR330006100519786457841326 and TR33 0006 1005 1978 6457 8413 26 "
        "and tr330006100519786457841326",
        "card 4242424242424242, 4242 4242 4242 4242 or 4242-4242-4242-4242",
    ],
    ids=["iban", "card"],
)
def test_one_value_in_three_layouts_keeps_one_number(written: str) -> None:
    decision = evaluate(written)

    assert len(decision.findings) == 3
    assert len(set(re.findall(r"<[A-Z_]+_[0-9]+>", decision.redacted_text))) == 1


def test_secrets_that_differ_only_in_case_are_two_values() -> None:
    decision = evaluate("password=Hunter2xyz! and later password=hunter2xyz!")

    assert decision.redacted_text.count("<SECRET_1>") == 1
    assert decision.redacted_text.count("<SECRET_2>") == 1
