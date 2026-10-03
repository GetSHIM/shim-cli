from __future__ import annotations

import base64
import importlib
import json
import os
import random
import signal
import string
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
        (later, first, Finding("EMAIL", 40, 50, 0.7, "")), "x", False, 0
    )

    assert decision.counts == (("PHONE", 1), ("EMAIL", 2))
    with pytest.raises((AttributeError, TypeError)):
        decision.redacted_text = "changed"  # type: ignore[misc]
    for count in (-1, True):
        with pytest.raises(ValueError):
            GuardDecision((), "x", False, count)


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


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("WHERE name LIKE '%admin%'", "WHERE name LIKE '%admin%'"),
        ("echo %DATE%", "echo %DATE%"),
        ("caf%E9", "caf%E9"),
        ("ali%C3%28ce", "ali%C3(ce"),
        ("%E9 ali%C3%A7e", "%E9 ali\u00e7e"),
    ),
)
def test_a_percent_escape_that_is_not_utf8_is_read_as_written(
    text: str, expected: str
) -> None:
    assert normalize(text).text == expected


def test_an_address_between_stray_percent_escapes_is_still_masked() -> None:
    decision = evaluate("caf%E9 ali%C3%A7e@example.com caf%E9")

    assert decision.redacted_text == "caf%E9 <EMAIL_1> caf%E9"


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
    "postgres://db.internal.example.com:5432/orders",
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
    looping forever on a zero-length slice, and the next one starts a little
    earlier so a value on the cut is read whole."""
    from shim_cli.guard.evaluate import _OVERLAP, _pieces

    text = "x" * (MAX_SOURCE_CHARACTERS * 2 + 5)
    cuts = list(_pieces(text))

    assert [offset for offset, _piece in cuts] == [
        0,
        MAX_SOURCE_CHARACTERS - _OVERLAP,
        2 * (MAX_SOURCE_CHARACTERS - _OVERLAP),
    ]
    assert cuts[-1][0] + len(cuts[-1][1]) == len(text)
    assert all(len(piece) <= MAX_SOURCE_CHARACTERS for _offset, piece in cuts)


def test_a_piece_that_fails_leaves_the_others_masked() -> None:
    import sys

    # `shim_cli.guard.evaluate` resolves to the re-exported function, not the
    # module it lives in.
    evaluate_module = sys.modules["shim_cli.guard.evaluate"]
    real = evaluate_module.analyze_counting
    calls = {"n": 0}

    def flaky(text, entities=(), custom=()):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("Guard input contains malformed percent encoding.")
        return real(text, entities, custom)

    line = "row %d contact user%d@example.com\n"
    text = "".join(line % (index, index) for index in range(14_000))
    try:
        evaluate_module.analyze_counting = flaky
        decision = evaluate(text, ("EMAIL",))
    finally:
        evaluate_module.analyze_counting = real

    assert decision.partial is True
    assert decision.findings, "one bad piece must not cost every other piece"
    assert "<EMAIL_1>" in decision.redacted_text


def test_a_piece_that_runs_out_of_time_ends_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyze_module = importlib.import_module("shim_cli.guard.analyze")
    evaluate_module = sys.modules["shim_cli.guard.evaluate"]
    real = evaluate_module.analyze_counting
    calls = []

    def counted(text, entities=(), custom=()):
        calls.append(len(text))
        return real(text, entities, custom)

    evaluate("Call +90 532 123 45 67 or write to alice@example.com")
    monkeypatch.setattr(analyze_module, "ANALYSIS_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(evaluate_module, "analyze_counting", counted)

    decision = evaluate(("x_token." * 12_000 + "\n") * 3)

    assert decision.partial is True
    assert len(calls) == 1


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


PHONE_SPANS = (
    ('{"created": 1757496600, "amount": 2000}', []),
    ('{"timestamp_ms": 1757496600123}', []),
    ("$ date +%s\n1757496600", []),
    ("3f9a2c1 1757496600 Fix parser timeout", []),
    ("-rw-r--r-- 1 dev staff 2147480000 1757496600 build.tar", []),
    ("Elapsed: 0.0376118499 s", []),
    ("mean=0.1234567890", []),
    ("2147483648", []),
    ("order_id 2026091012", []),
    ('"total_cost_usd": 0.03761184999', []),
    ('{"name": "Test User", "phone": "4155552671"}', [[32, 42]]),
    ("Telefon 5321234567", [[8, 18]]),
    ("7,Test User,Ankara,05321234567", [[19, 30]]),
    ("Reach the test desk at 5321234567 after lunch", [[23, 33]]),
    ("Tel: 0212 555 12 34", [[5, 19]]),
    ("Standard 01.23.45.67.89", [[9, 23]]),
    ("phone    4155552671", [[9, 19]]),
    ("phone     4155552671", []),
    ('"ts": 1757496600', []),
    ('"ts": 5321234567', [[6, 16]]),
    ("hotel 1757496600", []),
    ("Intel 4155552671", []),
    ("order_no: 4155552671", [[10, 20]]),
    ("phone number: 4155552671", [[14, 24]]),
    ("Telefon numarası 4155552671", [[17, 27]]),
    ("Reference number 1234567890", []),
    ("5321234567.25", []),
    ("x 0.05321234567", []),
    ("1757496600 0.0376118499 s", []),
)


@pytest.mark.parametrize(("text", "spans"), PHONE_SPANS)
def test_a_bare_number_is_a_phone_only_when_shaped_or_cued(
    text: str, spans: list
) -> None:
    findings = evaluate(text).findings

    assert [[f.start, f.end] for f in findings if f.entity_type == "PHONE"] == spans


def test_bare_numbers_counts_the_ids_no_recogniser_claimed() -> None:
    assert evaluate("ids 2147483648, 2026091012 and 1757496600").bare_numbers == 3
    assert evaluate("Elapsed 0.0376118499 s, mean 0.1234567890").bare_numbers == 0
    assert evaluate("Phone +90 532 123 45 67").bare_numbers == 0
    assert evaluate("ids 2147483648 2026091012", ("EMAIL",)).bare_numbers == 0
    assert evaluate("TCKN 12345678950", ("PHONE",)).bare_numbers == 1
    assert evaluate("TCKN 12345678950").bare_numbers == 0


@pytest.mark.parametrize(
    "token",
    ("xox" + "b-0000000000-EXAMPLEEXAMPLE", "AIza" + "0" * 35, "npm_" + "0" * 36),
)
def test_a_vendor_prefix_counts_only_as_a_whole_token(token: str) -> None:
    assert evaluate(f"rotate {token} today").counts == (("SECRET", 1),)
    assert evaluate(f"rotate id{token} today").counts == ()


def test_scanning_resumes_inside_a_value_it_dropped() -> None:
    text = "TOKEN_URL=https://x.example.com/?a=1&api_key=0123456789abcdef&b=2"

    assert evaluate(text).redacted_text == (
        "TOKEN_URL=https://x.example.com/?a=1&api_key=<SECRET_1>"
    )


def test_a_reference_under_a_bare_key_is_not_a_secret() -> None:
    assert evaluate("password=${DB_PASS}").counts == ()


@pytest.mark.parametrize(
    ("text", "value"),
    (
        ('credentials = Credentials(token="example-token-0000")', "example-token-0000"),
        (
            'db_credentials = dict(password="example-password-0000")',
            "example-password-0000",
        ),
        (
            'token_auth = HTTPBasicAuth(password="example-password-0000")',
            "example-password-0000",
        ),
        ('auth_token=Token(secret="example-secret-0000")', "example-secret-0000"),
        (
            'client_secret = get_secret(api_key="example-key-00000000")',
            "example-key-00000000",
        ),
        ('const credentials = {token: "example-token-0000"}', "example-token-0000"),
        ("credentials: {password: example-password-0000}", "example-password-0000"),
    ),
)
def test_a_secret_inside_a_newly_reached_value_is_still_masked(
    text: str, value: str
) -> None:
    assert value not in evaluate(text).redacted_text


@pytest.mark.parametrize(
    "text",
    (
        "DATABASE_URL=\nDB_PASSWORD=\nSTRIPE_SECRET_KEY=\nSLACK_BOT_TOKEN=\n"
        "NEXT_PUBLIC_API_URL=http://localhost:3000",
        "if not token_usage:\n    return None",
        "credentials:\n  username: admin",
    ),
)
def test_an_empty_value_does_not_take_the_next_line(text: str) -> None:
    assert evaluate(text).redacted_text == text


def test_a_1_0_2_key_still_reads_its_value_from_the_next_line() -> None:
    assert evaluate("password:\n  hunter22secret").counts == (("SECRET", 1),)


@pytest.mark.parametrize(
    "text",
    (
        "token_" * 16_000,
        "token=" * 16_000,
        "token_id=" * 11_000,
        "TOKEN_URL=https://a/" * 4_999,
        "x.token=" * 12_000,
    ),
    ids=("token_", "token=", "token_id=", "TOKEN_URL=", "x.token="),
)
def test_a_long_run_of_key_like_text_stays_fast(text: str) -> None:
    started = time.perf_counter()
    evaluate(text)

    assert time.perf_counter() - started < 1


@pytest.mark.parametrize(
    ("text", "value"),
    (
        ('password="${PREFIX}example-literal-0000"', "example-literal-0000"),
        ("password: ${PGPASS:-example-dev-password}", "example-dev-password"),
        ('secret="${VAULT_VALUE:-example-dev-password}"', "example-dev-password"),
    ),
)
def test_a_literal_beside_a_reference_is_still_masked(text: str, value: str) -> None:
    assert value not in evaluate(text).redacted_text


@pytest.mark.parametrize(
    "text",
    (
        "credentials.password=12345678",
        "secret.token=12345678",
        "app.credentials.api_key=12345678",
    ),
)
def test_a_dotted_key_ending_in_a_1_0_2_word_keeps_its_digits_masked(
    text: str,
) -> None:
    assert evaluate(text).counts == (("SECRET", 1),)


@pytest.mark.parametrize(
    "token",
    ("AIza" + "0" * 34, "AIza" + "0" * 36, "npm_" + "0" * 35, "npm_" + "0" * 37),
)
def test_a_fixed_length_vendor_token_needs_its_exact_length(token: str) -> None:
    assert evaluate(f"rotate {token} today").counts == ()


@pytest.mark.parametrize(
    "text",
    (
        "token_ids = tokenizer.encode(text)",
        '"token_type": "Bearer"',
        'api_key_header = "X-Api-Key"',
        '{"apiKeyName": "billing-key"}',
        "constructor(private tokenService: TokenService) {}",
        "tokenService = TokenService()",
        "SECRET_NAME=prod-db-password",
        'password_regex = "^(?=.*[A-Z]).{12,}$"',
        "client_secret = load_secret(path)",
        "Client(auth_token=self.auth_token)",
        "const authToken = session?.authToken;",
        'db_password = settings["DB_PASSWORD"]',
        "access_key: Option<String>,",
        "pub api_token: String,",
        "val signingKey: SigningKeyPair?",
        "private authToken: AuthToken;",
        "credentials: Optional[grpc.CallCredentials] = None",
        "refresh_token: Required[str]",
        "secret_backend: Mapped[str] = mapped_column()",
    ),
)
def test_code_that_names_a_secret_is_not_one(text: str) -> None:
    assert evaluate(text).counts == ()


@pytest.mark.parametrize(
    "text",
    (
        "DB_PASSWORD=Xk9(mP2qL7",
        "SMTP_PASSWORD=Sunshine",
        "ADMIN_PASSWORD=ChangeMe",
        "JWT_SECRET=SuperSecretKey",
        "SESSION_SECRET=KeyboardCat",
        "export DB_PASSWORD=CorrectHorseBatteryStaple",
        "DB_PASSWORD=abcXYZ(9mP2qL7",
        "DB_PASSWORD=Ab<9xQ!z",
        "DB_PASSWORD=Secret[2024]",
        "DB_PASSWORD=pass(word)x",
        "DB_PASSWORD=abc(def",
        "REDIS_PASSWORD: ChangeMeNow",
        "  adminPassword: BlueHarbor",
        "    JWT_SECRET: SuperSecret",
        "    REDIS_PASSWORD: RedisPassword",
        "  adminPassword: AdminPassword",
        "    API_TOKEN: MyToken",
        'SMTP_PASSWORD="correct.horse.battery"',
        "DISCORD_TOKEN=MTAwMDAwMDAwMDAwMDAwMDAw.GsYnTh.EXAMPLE0example0EXAMPLE0",
        "password = get_password()",
        "API_KEY_HEADER_VALUE=0123456789abcdef-synthetic",
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
    ),
)
def test_a_value_that_only_resembles_code_is_still_a_secret(text: str) -> None:
    assert evaluate(text).counts == (("SECRET", 1),)


def test_a_random_password_under_a_named_key_is_masked() -> None:
    symbols = "!#$&()*+-./:;<=>?@[^_`{|~"
    alphabet = string.ascii_letters + string.digits + symbols
    generator = random.Random(33)
    passwords = [
        "".join(generator.choice(alphabet) for _ in range(16)) for _ in range(2_000)
    ]

    missed = [
        password
        for password in passwords
        if not evaluate(f"DB_PASSWORD={password}").counts
    ]

    assert len(missed) <= 2, missed


def test_a_value_too_long_to_be_code_or_a_path_is_still_a_secret() -> None:
    assert evaluate("DB_PASSWORD=/" + "a/" * 600 + "key.pem").counts == (("SECRET", 1),)


@pytest.mark.parametrize(
    "text",
    (
        '{"auths": {"registry.example.com": {"auth": "Y2ktYm90OlN5bnRoZXRpYy0wMDAw"}}}',
        '"identitytoken": "c3ludGhldGljLWlkZW50aXR5LXRva2VuLTAwMDA="',
        '"identitytoken": "synthetic-identity_token.0000-AAAA"',
    ),
)
def test_a_docker_login_token_is_a_secret(text: str) -> None:
    assert evaluate(text).counts == (("SECRET", 1),)


@pytest.mark.parametrize("value", ("${DOCKER_IDENTITY_TOKEN}", "<your-identity-token>"))
def test_a_placeholder_identity_token_is_left_alone(value: str) -> None:
    text = f'"identitytoken": "{value}"'

    assert evaluate(text).redacted_text == text


def test_an_email_right_after_a_url_is_found_once_as_email() -> None:
    decision = evaluate("https://example.com/x alice@example.com")

    assert decision.counts == (("EMAIL", 1),)
    assert decision.redacted_text == "https://example.com/x <EMAIL_1>"


def test_a_long_run_of_schemes_stays_fast_and_still_masks_the_credential() -> None:
    text = (
        "DATABASE_URL=postgresql://app:synthetic-password@db.example.com/app\n"
        + "mysql://" * 12_000
    )

    started = time.perf_counter()
    decision = evaluate(text)

    assert time.perf_counter() - started < 2
    assert "synthetic-password" not in decision.redacted_text


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            "REDIS_URL=redis://:%40Synthetic1@redis:6379/0",
            "REDIS_URL=redis://<DB_URI_1>@redis:6379/0",
        ),
        ("redis://:!@Synthetic1@cache:6379", "redis://<DB_URI_1>@cache:6379"),
        ("postgres://:@hunter22@localhost/db", "postgres://<DB_URI_1>@localhost/db"),
        ("redis://:!@#$%^&*()@cache:6379", "redis://<DB_URI_1>@cache:6379"),
        ("https://:@Synthetic1@localhost:8080/", "https://<SECRET_1>@localhost:8080/"),
    ),
)
def test_the_whole_user_info_decides_whether_it_is_a_credential(
    text: str, expected: str
) -> None:
    assert evaluate(text).redacted_text == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            "DATABASE_URL=postgres://db.example.com/app?user=app&password=pw",
            "DATABASE_URL=postgres://db.example.com/app?user=app&password=<SECRET_1>",
        ),
        (
            "postgres://db.example.com/app?password=admin",
            "postgres://db.example.com/app?password=<SECRET_1>",
        ),
        ("redis://redis:6379?password=pw", "redis://redis:6379?password=<SECRET_1>"),
        (
            "jdbc:postgresql://db.example.com:5432/app?user=admin&password=root",
            "jdbc:postgresql://db.example.com:5432/app?user=admin&password=<SECRET_1>",
        ),
        (
            "postgres://db.example.com/app?db_password=12345678",
            "postgres://db.example.com/app?db_password=<SECRET_1>",
        ),
    ),
)
def test_a_short_query_password_in_a_connection_string_is_masked(
    text: str, expected: str
) -> None:
    assert evaluate(text).redacted_text == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            "postgres://user:synth%20etic@db:5432/app",
            "postgres://<DB_URI_1>%20etic@db:5432/app",
        ),
        (
            "postgres://user:synth'etic@db:5432/app",
            "postgres://<DB_URI_1>'etic@db:5432/app",
        ),
    ),
)
def test_a_password_cut_short_by_a_quote_or_escape_keeps_its_start_masked(
    text: str, expected: str
) -> None:
    assert evaluate(text).redacted_text == expected


@pytest.mark.parametrize(
    "text",
    (
        'url = f"https://{user}:{password}@{host}/api"',
        "const url = `https://${user}:${pass}@${host}/`",
        "postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/app",
        "postgresql://***@db-prod.kasa.internal:5432/kasa",
        "postgresql://HOST:5432/DATABASE",
    ),
)
def test_user_info_made_of_references_or_elisions_is_not_a_credential(
    text: str,
) -> None:
    assert evaluate(text).redacted_text == text


def test_the_hook_deadline_ends_the_scan_of_later_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shim_cli.events.payload import inspect

    hook = importlib.import_module("shim_cli.hook")
    analyze_module = importlib.import_module("shim_cli.guard.analyze")
    real = analyze_module.analyze_text
    calls = []

    def slow(text, entities, custom):
        calls.append(len(text))
        time.sleep(2)
        return real(text, entities, custom)

    monkeypatch.setattr(hook, "HOOK_DEADLINE_SECONDS", 0.3)
    monkeypatch.setattr(analyze_module, "ANALYSIS_DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(analyze_module, "analyze_text", slow)
    leaf = "x " * 60_000

    with hook._deadline():
        result = inspect({"tool_response": {"a": leaf, "b": leaf, "c": leaf}}, evaluate)

    assert "deadline" in result.reasons
    assert len(calls) == 1


KOREAN = "한국어 문장을 길게 붙여 넣습니다. 각 줄은 평범한 설명입니다.\n"


@pytest.mark.parametrize("length", (97_000, 110_000), ids=("one piece", "two pieces"))
def test_korean_text_that_expands_past_the_limit_is_scanned_whole(length: int) -> None:
    text = "DB_PASSWORD=Synthetic-pass-0000\n" + (KOREAN * 3_000)[:length]

    decision = evaluate(text)

    assert decision.partial is False
    assert decision.counts == (("SECRET", 1),)


def test_the_hook_deadline_is_handed_back_after_an_analysis_in_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = importlib.import_module("shim_cli.hook")
    monkeypatch.setattr(hook, "HOOK_DEADLINE_SECONDS", 30)

    with hook._deadline():
        evaluate("DB_PASSWORD=Synthetic-pass-0000")
        left = signal.getitimer(signal.ITIMER_REAL)[0]

    assert 25 < left <= 30


@pytest.mark.parametrize(
    ("value", "masked", "placeholder"),
    (
        ("synthetic.user@example.com", "synthetic.user@example.com", "<EMAIL_1>"),
        ("DB_PASSWORD=Synthetic-pass-0000", "Synthetic-pass-0000", "<SECRET_1>"),
        (
            "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
            "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
            "<SECRET_1>",
        ),
    ),
    ids=("email", "named secret", "token"),
)
def test_a_value_across_a_cut_in_one_long_line_is_found(
    value: str, masked: str, placeholder: str
) -> None:
    head, tail = "x " * 49_995, " " + "y " * 60_000

    decision = evaluate(head + value + tail)
    redacted = decision.redacted_text

    assert decision.partial is False
    assert redacted.startswith(head)
    assert redacted.endswith(tail)
    assert redacted[len(head) : -len(tail)] == value.replace(masked, placeholder)


def test_a_value_inside_the_overlap_is_counted_once() -> None:
    text = "x " * 48_500 + "synthetic.user@example.com " + "y " * 60_000

    decision = evaluate(text)

    assert decision.counts == (("EMAIL", 1),)
    assert decision.redacted_text.count("<EMAIL_1>") == 1


def test_a_value_across_the_half_of_a_long_korean_line_is_found() -> None:
    line = (KOREAN.replace("\n", " ") * 3_000)[:49_990]
    text = line + " DB_PASSWORD=Synthetic-pass-0000 " + line + line[:10_000]

    decision = evaluate(text)

    assert decision.partial is False
    assert decision.counts == (("SECRET", 1),)


def test_a_connection_string_on_the_overlap_start_keeps_its_password_masked() -> None:
    uri = (
        "postgres://app:Synthetic-pass-7007@mydb.cluster-abc123xyz"
        ".eu-central-1.rds.amazonaws.com:5432/app"
    )
    text = "x " * 47_940 + uri + " " + "y " * 60_000
    assert text.index(uri) + 15 < 100_000 - 4_096 < text.index(uri) + 34

    decision = evaluate(text)

    assert "Synthetic" not in decision.redacted_text
    assert "pass-7007" not in decision.redacted_text
    assert dict(decision.counts) == {"DB_URI": 1}


ENV_TEXT = "APP_ENV=production\nDB_PASSWORD=Synthetic-pass-0000\nOPS=ops@example.com\n"
STRADDLING = base64.b64encode(
    b"APP_ENV=production\nAPP_NAME=billing-service\nDB_PASSWORD=Synthetic-pass-0000\n"
).decode()
FULL_LINE = base64.b64encode(
    b"DB_PASSWORD=Synthetic-pass-0000\nAPP_ENV=production-abcde\n"
).decode()


def _wrapped(encoded: str, width: int, separator: str = "\n") -> str:
    return separator.join(
        encoded[start : start + width] for start in range(0, len(encoded), width)
    )


@pytest.mark.parametrize("width", (0, 60, 64, 76), ids=("one line", "60", "64", "76"))
def test_a_secret_sent_as_base64_is_masked_as_a_whole(width: int) -> None:
    encoded = base64.b64encode(ENV_TEXT.encode()).decode()
    if width:
        encoded = _wrapped(encoded, width)

    decision = evaluate(f"$ base64 .env\n{encoded}\n")

    assert decision.redacted_text == "$ base64 .env\n<SECRET_1>\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            f"data:\n  .env: |\n    {_wrapped(STRADDLING, 76, chr(10) + '    ')}\n",
            "data:\n  .env: |\n    <SECRET_1>\n",
        ),
        (f"{_wrapped(STRADDLING, 76, '   ' + chr(10))}   \n", "<SECRET_1>   \n"),
        (f"{_wrapped(STRADDLING, 76, chr(13) + chr(10))}\r\n", "<SECRET_1>\r\n"),
        (f"{_wrapped(STRADDLING, 76, chr(13))}\r", "<SECRET_1>\r"),
        (
            '{"content": "' + _wrapped(STRADDLING, 60, "\\n") + '\\n"}',
            '{"content": "<SECRET_1>\\n"}',
        ),
        (
            '{"output": "$ base64 .env\\n' + STRADDLING + '\\n"}',
            '{"output": "$ base64 .env\\n<SECRET_1>\\n"}',
        ),
        (
            f"helm/values.yaml:5:    {STRADDLING[:76]}\n"
            f"helm/values.yaml:6:    {STRADDLING[76:]}\n",
            "helm/values.yaml:5:    <SECRET_1>\n",
        ),
        (
            f"     5\t    {STRADDLING[:76]}\n     6\t    {STRADDLING[76:]}\n",
            "     5\t    <SECRET_1>\n",
        ),
        (
            f"2:envFile: |\n3-    {STRADDLING[:76]}\n4-    {STRADDLING[76:]}\n5-x: 1\n",
            "2:envFile: |\n3-    <SECRET_1>\n5-x: 1\n",
        ),
        (
            f"helm/values.yaml-3-    {STRADDLING[:76]}\n"
            f"helm/values.yaml-4-    {STRADDLING[76:]}\n",
            "helm/values.yaml-3-    <SECRET_1>\n",
        ),
        (
            f"My Project/values.yaml:3:    {STRADDLING[:76]}\n"
            f"My Project/values.yaml:4:    {STRADDLING[76:]}\n",
            "My Project/values.yaml:3:    <SECRET_1>\n",
        ),
        (f"@@ -0,0 +1 @@\n+{STRADDLING}\n", "@@ -0,0 +1 @@\n+<SECRET_1>\n"),
        (f"+{STRADDLING[:76]}\n+{STRADDLING[76:]}\n", "+<SECRET_1>\n"),
        (
            f"+  envFile: |\n+    {STRADDLING[:76]}\n+    {STRADDLING[76:]}\n",
            "+  envFile: |\n+    <SECRET_1>\n",
        ),
        (f"-    {STRADDLING[:76]}\n-    {STRADDLING[76:]}\n", "-    <SECRET_1>\n"),
        ('{"row": "id\\t' + STRADDLING + '"}', '{"row": "id\\t<SECRET_1>"}'),
        (
            '{"content": "' + _wrapped(STRADDLING, 76, "\\r\\n") + '\\r\\n"}',
            '{"content": "<SECRET_1>\\r\\n"}',
        ),
        (
            '{"content": "' + _wrapped(STRADDLING, 60, "\\n") + '"}',
            '{"content": "<SECRET_1>"}',
        ),
        (
            '{"out": "helm/values.yaml:5:    '
            + STRADDLING[:76]
            + "\\nhelm/values.yaml:6:    "
            + STRADDLING[76:]
            + '\\n"}',
            '{"out": "helm/values.yaml:5:    <SECRET_1>\\n"}',
        ),
    ),
    ids=(
        "indented YAML",
        "trailing spaces",
        "CRLF",
        "CR",
        "JSON-escaped lines",
        "after a JSON-escaped line",
        "grep -n",
        "cat -n",
        "grep -A",
        "rg context",
        "a path with a space",
        "git diff",
        "git diff wrapped",
        "git diff indented",
        "git diff deleted",
        "after an escaped tab",
        "escaped CRLF",
        "ends at the closing quote",
        "grep -n in JSON",
    ),
)
def test_a_secret_split_across_wrapped_lines_is_masked(
    text: str, expected: str
) -> None:
    assert evaluate(text).redacted_text == expected


@pytest.mark.parametrize(
    "prefix",
    (
        "ENV_FILE_B64=",
        "export ENV_FILE_B64=",
        "kubectl create secret generic app --from-literal=env=",
    ),
)
def test_base64_right_after_an_equals_sign_is_read(prefix: str) -> None:
    assert evaluate(prefix + STRADDLING).redacted_text == prefix + "<SECRET_1>"


def test_base64_without_its_padding_is_read() -> None:
    assert STRADDLING.endswith("==")

    assert evaluate(f"value: {STRADDLING.rstrip('=')}").redacted_text == (
        "value: <SECRET_1>"
    )


def test_base64_of_text_that_is_not_utf8_is_still_read() -> None:
    text = "# Türkçe açıklama\nDB_PASSWORD=Synthetic-pass-0000\n".encode("cp1254")

    assert evaluate(f"value: {base64.b64encode(text).decode()}").redacted_text == (
        "value: <SECRET_1>"
    )


@pytest.mark.parametrize(
    "after", ("Done.", "config/settings.py:1:import os", "abcd efgh")
)
def test_a_full_base64_line_is_not_joined_with_the_line_after_it(after: str) -> None:
    assert len(FULL_LINE) == 76

    decision = evaluate(f"k8s/env.yaml:7:  data: {FULL_LINE}\n{after}")

    assert decision.redacted_text == f"k8s/env.yaml:7:  data: <SECRET_1>\n{after}"


PADDED = base64.b64encode(
    b"DB_PASSWORD=Synthetic-pass-0000\nAPP_ENV=production-abcd\n"
).decode()
TOKEN_BLOCK = base64.b64encode(
    b"GITHUB_TOKEN=ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n"
).decode()
CLEAN_BLOCK = base64.b64encode(
    b"APP_ENV=staging\nAPP_NAME=billing-service\nLOG_LEVEL=info\nREGION=eu-west-1\n"
).decode()


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (f"{PADDED}\n{TOKEN_BLOCK}\n", "<SECRET_1>\n<SECRET_2>\n"),
        (
            f"config/prod.env.b64:1:{PADDED}\n"
            f"config/staging.env.b64:1:{CLEAN_BLOCK[:76]}\n"
            f"config/staging.env.b64:2:{CLEAN_BLOCK[76:]}\n",
            f"config/prod.env.b64:1:<SECRET_1>\n"
            f"config/staging.env.b64:1:{CLEAN_BLOCK[:76]}\n"
            f"config/staging.env.b64:2:{CLEAN_BLOCK[76:]}\n",
        ),
    ),
    ids=("two blocks", "a grep listing"),
)
def test_padding_ends_a_block(text: str, expected: str) -> None:
    assert len(PADDED) == 76
    assert PADDED.endswith("=")

    assert evaluate(text).redacted_text == expected


LOGIN = "Y2ktYm90OlN5bnRoZXRpYy1yZWdpc3RyeS0wMDAw"
LOGIN_WITH_NEWLINE = base64.b64encode(b"ci-bot:Synthetic-registry-0000\n").decode()


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            f"//registry.example.com/:_auth={LOGIN}\n",
            "//registry.example.com/:_auth=<SECRET_1>\n",
        ),
        (f'_auth="{LOGIN}"\n', '_auth="<SECRET_1>"\n'),
        (f"_auth='{LOGIN}'\n", "_auth='<SECRET_1>'\n"),
        (f"export NPM_CONFIG__AUTH={LOGIN}\n", "export NPM_CONFIG__AUTH=<SECRET_1>\n"),
        (f'{{"auth": "{LOGIN_WITH_NEWLINE}"}}', '{"auth": "<SECRET_1>"}'),
    ),
    ids=(
        "npm",
        "npm quoted",
        "npm single quotes",
        "npm environment",
        "docker with a newline",
    ),
)
def test_a_login_in_base64_is_a_secret(text: str, expected: str) -> None:
    assert evaluate(text).redacted_text == expected


@pytest.mark.parametrize(
    "value", ("required", "anonymous", "keycloak", "basicAuth", "anVzdC1hLXRva2Vu")
)
def test_an_auth_setting_that_is_not_a_login_is_left_alone(value: str) -> None:
    text = f'{{"route": "/admin", "auth": "{value}"}}'

    assert evaluate(text).redacted_text == text


def test_lines_of_another_width_are_not_joined() -> None:
    secret = base64.b64encode(ENV_TEXT.encode() + b"x" * 1).decode()
    noise = base64.b64encode(bytes(range(200, 256)) + bytes(range(16))).decode()
    assert len(secret) == len(noise) == 96

    decision = evaluate(f"{secret}\n{noise}\nabcd")

    assert decision.redacted_text == f"<SECRET_1>\n{noise}\nabcd"


@pytest.mark.parametrize(
    "config",
    (
        '{"auths":{"registry.example.com":{"password":"Synthetic-registry-0000"}}}',
        '{"auths":{"registry.example.com":'
        '{"auth":"Y2ktYm90OlN5bnRoZXRpYy1yZWdpc3RyeS0wMDAw"}}}',
    ),
    ids=("password", "docker login"),
)
def test_a_registry_login_inside_a_kubernetes_secret_is_masked(config: str) -> None:
    text = f"  .dockerconfigjson: {base64.b64encode(config.encode()).decode()}"

    assert evaluate(text).redacted_text == "  .dockerconfigjson: <SECRET_1>"


@pytest.mark.parametrize(
    "value",
    (
        base64.b64encode(b"just a sentence with nothing secret in it").decode(),
        base64.b64encode(bytes(range(48))).decode(),
        base64.b64encode(bytes(range(128, 256))).decode(),
        "AbstractSingletonProxyFactoryBeanImplementation",
    ),
    ids=("plain text", "control characters", "not UTF-8", "identifier"),
)
def test_base64_that_holds_no_secret_is_left_alone(value: str) -> None:
    assert evaluate(f"value: {value}").counts == ()


def test_a_long_base64_blob_is_scanned_for_emails_quickly() -> None:
    blob = base64.b64encode(random.Random(7).randbytes(74_997)).decode()

    started = time.perf_counter()
    evaluate(blob, ("EMAIL",))

    assert time.perf_counter() - started < 1


def test_json_escaped_base64_is_scanned_in_linear_time() -> None:
    encoded = base64.b64encode(random.Random(1).randbytes(225_000)).decode()
    text = _wrapped(encoded, 60, "\\n")[:297_000]

    started = time.perf_counter()
    evaluate(text, ("SECRET",))

    assert time.perf_counter() - started < 1


def test_a_diff_that_adds_a_long_base64_file_is_scanned_quickly() -> None:
    encoded = base64.b64encode(random.Random(3).randbytes(72_000)).decode()
    lines = [f"+{encoded[start : start + 64]}" for start in range(0, len(encoded), 64)]
    text = "@@ -0,0 +1,1500 @@\n" + "\n".join(lines) + "\n"

    started = time.perf_counter()
    evaluate(text, ("SECRET",))

    assert time.perf_counter() - started < 1


@pytest.mark.parametrize("prefix", ("data: ", "data:  ", "data:   ", "data:    "))
def test_a_long_base64_block_across_a_cut_is_masked_whole(prefix: str) -> None:
    env = "".join(
        f"SERVICE_{n:05d}_PASSWORD=Synthetic-pass-{n:05d}\n" for n in range(2_500)
    )
    block = base64.b64encode(env.encode()).decode()

    decision = evaluate(f"{prefix}{block}\n")

    assert len(decision.redacted_text) == len(prefix) + len("<SECRET_1>\n")
    assert decision.redacted_text == f"{prefix}<SECRET_1>\n"
