from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from shim_cli.guard import (
    MAX_SOURCE_CHARACTERS,
    Finding,
    GuardDecision,
    analyze,
    evaluate,
)
from shim_cli.guard.normalize import normalize
from shim_cli.guard.recognizers import Match

CORPUS = json.loads(
    (Path(__file__).parents[1] / "corpus" / "guard-v2.json").read_text(encoding="utf-8")
)


def test_models_are_immutable_and_counts_follow_first_source_occurrence() -> None:
    later = Finding("EMAIL", 20, 30, 0.9, "")
    first = Finding("PHONE", 0, 10, 0.8, "")
    decision = GuardDecision(
        (later, first, Finding("EMAIL", 40, 50, 0.7, "")), "x", False
    )

    assert decision.counts == (("PHONE", 1), ("EMAIL", 2))
    with pytest.raises((AttributeError, TypeError)):
        decision.redacted_text = "changed"  # type: ignore[misc]


def test_ordinals_are_per_category_in_source_order_without_a_value_map() -> None:
    decision = evaluate("alice@example.com +90 532 123 45 67 bob@example.com")

    assert decision.redacted_text == "<EMAIL_1> <PHONE_1> <EMAIL_2>"
    assert decision.counts == (("EMAIL", 2), ("PHONE", 1))
    assert not hasattr(decision, "replacement_map")
    assert not hasattr(decision, "raw_values")


def test_evaluation_runs_only_selected_entities() -> None:
    text = "alice@example.com +90 532 123 45 67"

    decision = evaluate(text, ("PHONE",))

    assert [finding.entity_type for finding in decision.findings] == ["PHONE"]
    assert decision.redacted_text == "alice@example.com <PHONE_1>"
    with pytest.raises(ValueError, match="unsupported entity"):
        evaluate(text, ("NOT_AN_ENTITY",))


def test_source_and_normalized_intermediate_limits() -> None:
    generated = {case["id"]: case for case in CORPUS["generated_cases"]}
    oversized = (
        generated["source-oversize"]["value"] * generated["source-oversize"]["count"]
    )
    with pytest.raises(ValueError, match="safe analysis limit"):
        analyze(oversized)
    assert analyze(oversized, ()) == ()
    with pytest.raises(ValueError, match="safe analysis limit"):
        normalize(
            generated["normalization-intermediate-oversize"]["value"]
            * generated["normalization-intermediate-oversize"]["count"]
        )


def test_dense_findings_are_all_maskable() -> None:
    text = " ".join(f"u{index}@e.co" for index in range(9_000))

    assert len(text) <= MAX_SOURCE_CHARACTERS
    decision = evaluate(text, ("EMAIL",))

    assert decision.counts == (("EMAIL", 9_000),)
    assert decision.redacted_text.startswith("<EMAIL_1>")
    assert decision.redacted_text.endswith("<EMAIL_9000>")
    assert "@e.co" not in decision.redacted_text


def test_invalid_or_incomplete_analyzer_spans_fail_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("shim_cli.guard.analyze")

    def out_of_range(_text: str, _entities: tuple[str, ...], _custom=()) -> list[Match]:
        return [Match("EMAIL_ADDRESS", 0, 999, 0.9)]

    monkeypatch.setattr(module, "analyze_text", out_of_range)
    with pytest.raises(ValueError, match="invalid span"):
        module.analyze("safe")


def test_shared_analysis_deadline_fails_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("shim_cli.guard.analyze")

    def slow(_text: str, _entities: tuple[str, ...], _custom=()) -> list[Match]:
        time.sleep(1)
        return []

    monkeypatch.setattr(module, "ANALYSIS_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(module, "analyze_text", slow)
    with pytest.raises(ValueError, match="runtime limit"):
        module.analyze("safe")


def test_adversarial_punctuation_completes_within_the_detector_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("shim_cli.guard.analyze")
    monkeypatch.setattr(module, "ANALYSIS_DEADLINE_SECONDS", 3)

    started = time.monotonic()
    findings = module.analyze('"' * 12_000 + " alice@example.com")

    assert time.monotonic() - started < 3
    assert [finding.entity_type for finding in findings] == ["EMAIL"]


def test_malformed_percent_encoded_utf8_fails_safely() -> None:
    with pytest.raises(ValueError, match="malformed percent encoding"):
        analyze("alice%C3%28@example.com")


def test_overlap_tie_is_deterministic_and_covers_the_component() -> None:
    module = importlib.import_module("shim_cli.guard.analyze")
    resolved = module._resolve_overlaps(
        [
            Finding("TR_VKN", 0, 8, 0.8, ""),
            Finding("TR_NATIONAL_ID", 4, 12, 0.8, ""),
        ]
    )

    assert resolved == [Finding("TR_NATIONAL_ID", 0, 12, 0.8, "")]


def test_email_validation_reads_neither_the_network_nor_the_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("email validation attempted I/O")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr("io.open", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)

    assert analyze("alice@example.com")[0].entity_type == "EMAIL"
    assert analyze("alice@example.invalid") == ()


def test_public_suffix_rules_match_the_behaviour_they_replaced() -> None:
    from shim_cli.guard.suffixes import is_registrable

    assert is_registrable("example.com")
    assert is_registrable("a.b.c.example.co.uk")
    assert is_registrable("example.xn--p1ai")
    assert is_registrable("example.\u0440\u0444")
    assert is_registrable("blogspot.com")
    assert is_registrable("www.ck")
    assert not is_registrable("example.invalid")
    assert not is_registrable("localhost")
    assert not is_registrable("co.uk")
    assert not is_registrable("foo.ck")
    assert not is_registrable("example..com")
    assert not is_registrable("")


def test_results_are_independent_of_python_hash_seed() -> None:
    case = next(
        case
        for case in CORPUS["generated_cases"]
        if case["id"] == "deterministic-hash-seed"
    )
    command = [
        sys.executable,
        "-c",
        "from shim_cli.guard import evaluate; print(evaluate('alice@example.com 192.168.1.1'))",
    ]
    outputs = []
    for seed in case["seeds"]:
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        outputs.append(
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            ).stdout
        )
    assert outputs[0] == outputs[1]


QUIET = (
    "127.0.0.1",
    "127.0.1.1",
    "0.0.0.0",
    "::1",
    "::",
    "redis://localhost:6379/0",
    "postgresql://localhost/mydb",
    "mongodb://127.0.0.1:27017",
    "redis://[::1]:6379",
    'REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")',
    "the server listens on 0.0.0.0:8080 in production",
)

LOUD = (
    ("10.0.0.5", "IP_ADDRESS"),
    ("192.168.1.44", "IP_ADDRESS"),
    ("172.16.0.1", "IP_ADDRESS"),
    ("8.8.8.8", "IP_ADDRESS"),
    ("203.0.113.9", "IP_ADDRESS"),
    ("2001:db8::8a2e:370:7334", "IP_ADDRESS"),
    ("postgres://user:pw@localhost/db", "DB_URI"),
    ("redis://:hunter2@localhost:6379", "DB_URI"),
    ("postgres://admin@localhost/db", "DB_URI"),
    ("postgres://db.internal.example.com:5432/orders", "DB_URI"),
    ("postgres://rw:0123456789abcdef@db.internal.example.com:5432/orders", "DB_URI"),
    ("mysql://user:pw@host/db", "DB_URI"),
)


@pytest.mark.parametrize("text", QUIET)
def test_addresses_that_name_nobody_are_left_alone(text: str) -> None:
    decision = evaluate(text)

    assert decision.counts == (), decision.redacted_text
    assert decision.redacted_text == text


@pytest.mark.parametrize(("text", "entity"), LOUD)
def test_a_credential_or_a_real_host_is_still_caught(text: str, entity: str) -> None:
    decision = evaluate(text)

    assert entity in dict(decision.counts), decision.counts
    assert text not in decision.redacted_text


def _custom(*entries):
    from shim_cli.guard.entities import compile_custom

    return compile_custom(entries)


def test_a_custom_pattern_is_masked_and_numbered_like_any_other_type() -> None:
    from shim_cli.guard import evaluate

    patterns = _custom({"name": "CODENAME", "pattern": r"ATLAS-[0-9]{4}"})

    decision = evaluate("ATLAS-0042 and ATLAS-0043", custom=patterns)

    assert decision.redacted_text == "<CUSTOM_1> and <CUSTOM_2>"
    assert decision.counts == (("CUSTOM", 2),)
    assert decision.custom_counts == (("CODENAME", 2),)


def test_the_pattern_name_never_reaches_the_placeholder() -> None:
    from shim_cli.guard import evaluate

    patterns = _custom({"name": "SECRET_PROJECT_NAME", "literal": "atlas"})

    decision = evaluate("atlas ships", custom=patterns)

    assert "SECRET_PROJECT_NAME" not in decision.redacted_text
    assert decision.findings[0].label == "SECRET_PROJECT_NAME"


def test_a_built_in_type_wins_an_overlapping_custom_span() -> None:
    from shim_cli.guard import evaluate

    patterns = _custom({"name": "ANYTHING", "pattern": r"[a-z.@]+"})

    decision = evaluate("alice@example.com", custom=patterns)

    assert decision.redacted_text == "<EMAIL_1>"
    assert decision.custom_counts == ()


def test_two_patterns_are_counted_under_their_own_names() -> None:
    from shim_cli.guard import evaluate

    patterns = _custom(
        {"name": "CODENAME", "literal": "atlas"},
        {"name": "HOST", "pattern": r"\b[a-z]+\.corp\.internal\b"},
    )

    decision = evaluate("atlas on build.corp.internal", custom=patterns)

    assert dict(decision.custom_counts) == {"CODENAME": 1, "HOST": 1}


def test_no_configured_patterns_means_no_custom_findings() -> None:
    from shim_cli.guard import evaluate

    decision = evaluate("ATLAS-0042 ships")

    assert decision.findings == ()
    assert decision.custom_counts == ()


def test_scan_custom_reports_the_span_it_matched() -> None:
    from shim_cli.guard.recognizers import scan_custom

    patterns = _custom({"name": "CODENAME", "literal": "atlas"})

    matches = scan_custom("say atlas now", patterns)

    assert [(m.entity_type, m.start, m.end, m.label) for m in matches] == [
        ("CUSTOM", 4, 9, "CODENAME")
    ]


def test_a_backtracking_pattern_is_named_before_it_can_run() -> None:
    from shim_cli.guard.entities import unsafe_pattern

    assert unsafe_pattern("BAD", "(a+)+$").startswith("pattern BAD backtracks")
    assert unsafe_pattern("BACKREF", r"(a)\1").startswith("pattern BACKREF backtracks")
    assert unsafe_pattern("BROKEN", "(unclosed") == (
        "pattern BROKEN is not a valid regular expression"
    )
    assert unsafe_pattern("FINE", r"\bATLAS-[0-9]{4}\b") == ""


def test_a_tail_shorter_than_asked_for_is_what_there_is() -> None:
    from shim_cli.guard.evaluate import _tail

    assert _tail("TR33 0006 1005 1978 6457 8413 26", 4) == "1326"
    assert _tail("ab1", 4) == "1"
    assert _tail("no digits here", 4) == ""


def test_a_span_with_no_digits_keeps_the_whole_placeholder() -> None:
    from shim_cli.guard import evaluate
    from shim_cli.guard.entities import compile_custom

    patterns = compile_custom([{"name": "CODENAME", "literal": "atlas"}])

    decision = evaluate("atlas ships", custom=patterns, reveal={"CUSTOM": 4})

    assert decision.redacted_text == "<CUSTOM_1> ships"


def test_the_one_placeholder_pattern_matches_both_forms_and_nothing_else() -> None:
    from shim_cli.guard import PLACEHOLDER

    assert PLACEHOLDER.fullmatch("<IBAN_1>")
    assert PLACEHOLDER.fullmatch("<IBAN_12:1326>")
    assert PLACEHOLDER.fullmatch("<CREDIT_CARD_1:4>")
    assert not PLACEHOLDER.fullmatch("<IBAN_1:12345>")
    assert not PLACEHOLDER.fullmatch("<iban_1>")
    assert not PLACEHOLDER.fullmatch("<IBAN>")
    assert not PLACEHOLDER.fullmatch("<IBAN_1:abcd>")


def test_normalize_handles_non_ascii_and_maps_spans_back_to_the_source() -> None:
    """`zip(strict=True)` here made every non-ASCII prompt raise on Python 3.9.

    The archive ships to a stock macOS, whose `python3` is 3.9, and the ASCII
    fast path above meant no fixture reached this code. The pinned values below
    assert the behaviour on any interpreter, so 3.9's absence cannot hide it.
    """
    plain = normalize("Hesap ışği: TR330006100519786457841326")
    assert plain.text == "Hesap ışği: TR330006100519786457841326"
    assert plain.source_spans[:3] == ((0, 1), (1, 2), (2, 3))
    assert len(plain.source_spans) == len(plain.text)

    # A ligature decomposes into two characters that both point at one source
    # character; a percent escape does the reverse.
    ligature = normalize("ﬁnans ışği")
    assert ligature.text == "finans ışği"
    assert ligature.source_spans[:2] == ((0, 1), (0, 1))

    escaped = normalize("ışğı %41 arttı")
    assert escaped.text == "ışğı A arttı"
    assert escaped.source_spans[5] == (5, 8)


def test_a_turkish_sentence_masks_only_the_iban() -> None:
    """The spans above are what puts the placeholder over the right characters."""
    decision = evaluate("Hesap ışği — TR330006100519786457841326 numaralı")

    assert decision.redacted_text == "Hesap ışği — <IBAN_1> numaralı"
    assert decision.counts == (("IBAN", 1),)


def test_a_field_over_the_scan_limit_is_masked_in_pieces() -> None:
    """A 150 KB tool result used to reach the model with every secret in it:
    `normalize()` refuses more than MAX_SOURCE_CHARACTERS and the caller passed
    the whole leaf, so nothing at all was masked."""
    line = "row %d contact user%d@example.com\n"
    text = "".join(line % (index, index) for index in range(14_000))
    assert len(text) > MAX_SOURCE_CHARACTERS * 4

    decision = evaluate(text, ("EMAIL",))

    assert len(decision.findings) == 14_000
    assert "@example.com" not in decision.redacted_text
    assert decision.partial is False
    # Numbering continues across the pieces rather than restarting at 1.
    assert "<EMAIL_1>" in decision.redacted_text
    assert "<EMAIL_14000>" in decision.redacted_text
    assert decision.redacted_text.count("<EMAIL_1>") == 1


def test_pieces_are_cut_on_newlines_so_no_line_is_split() -> None:
    from shim_cli.guard.evaluate import _pieces

    text = "".join(f"{index:06d} padding\n" for index in range(30_000))
    cuts = list(_pieces(text))

    assert len(cuts) > 1
    assert "".join(piece for _offset, piece in cuts) == text
    assert all(piece.endswith("\n") for _offset, piece in cuts[:-1])
    assert [offset for offset, _piece in cuts] == [0] + [
        sum(len(piece) for _o, piece in cuts[:index]) for index in range(1, len(cuts))
    ]


def test_a_line_longer_than_the_limit_still_makes_progress() -> None:
    """No newline to cut on: the piece ends at the hard boundary instead of
    looping forever on a zero-length slice."""
    from shim_cli.guard.evaluate import _pieces

    text = "x" * (MAX_SOURCE_CHARACTERS * 2 + 5)
    cuts = list(_pieces(text))

    assert [len(piece) for _offset, piece in cuts] == [
        MAX_SOURCE_CHARACTERS,
        MAX_SOURCE_CHARACTERS,
        5,
    ]


def test_a_piece_that_fails_leaves_the_others_masked() -> None:
    import sys

    # `shim_cli.guard.evaluate` resolves to the re-exported function, not the
    # module it lives in.
    evaluate_module = sys.modules["shim_cli.guard.evaluate"]
    real = evaluate_module.analyze
    calls = {"n": 0}

    def flaky(text, entities=(), custom=()):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("Guard input contains malformed percent encoding.")
        return real(text, entities, custom)

    line = "row %d contact user%d@example.com\n"
    text = "".join(line % (index, index) for index in range(14_000))
    try:
        evaluate_module.analyze = flaky
        decision = evaluate(text, ("EMAIL",))
    finally:
        evaluate_module.analyze = real

    assert decision.partial is True
    assert decision.findings, "one bad piece must not cost every other piece"
    assert "<EMAIL_1>" in decision.redacted_text


def test_a_config_line_keeps_its_variable_name() -> None:
    """The email local part accepted `=`, so `SUPPORT_EMAIL=ops@x.com` masked to
    a bare `<EMAIL_1>` while `AWS_ACCESS_KEY_ID=` kept its name. One `.env` came
    back in two shapes and the model lost which address belonged to which
    setting."""
    text = (
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
        "SUPPORT_EMAIL=ops@example.com\n"
        "CONTACT = alice@example.com\n"
    )

    assert evaluate(text).redacted_text == (
        "AWS_ACCESS_KEY_ID=<SECRET_1>\nSUPPORT_EMAIL=<EMAIL_1>\nCONTACT = <EMAIL_2>\n"
    )


def test_a_url_carrying_an_address_keeps_its_url() -> None:
    """Same cause: `?e=` was read as part of the local part, so the host and
    path vanished into the placeholder."""
    decision = evaluate("See https://example.com/u?e=alice@example.com now")

    assert decision.redacted_text == "See https://example.com/u?e=<EMAIL_1> now"
