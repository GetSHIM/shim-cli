"""U23: key blocks, vendor prefixes, bare SSNs, long cards, the text as
written, and the custom-pattern safety check."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from urllib.parse import quote

import pytest

from shim_cli.guard import evaluate
from shim_cli.guard.entities import PROBE_MILLISECONDS, compile_custom, unsafe_pattern

BODY = "MIIBuwIBAAKBgQD0123456789abcdef0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    "block",
    (
        f"-----BEGIN DSA PRIVATE KEY-----\n{BODY}\n-----END DSA PRIVATE KEY-----",
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\n\n"
        f"{BODY}\n=AbCd\n-----END PGP PRIVATE KEY BLOCK-----",
        "---- BEGIN SSH2 ENCRYPTED PRIVATE KEY ----\n"
        f'Comment: "synthetic"\n{BODY}\n---- END SSH2 ENCRYPTED PRIVATE KEY ----',
        f"-----BEGIN ENCRYPTED PRIVATE KEY-----\n{BODY}\n"
        "-----END ENCRYPTED PRIVATE KEY-----",
        f"-----BEGIN DSA PRIVATE KEY-----\n{BODY}\n",
    ),
)
def test_every_private_key_block_is_one_secret(block: str) -> None:
    decision = evaluate(f"key:\n{block}\nafter")

    assert [f.entity_type for f in decision.findings] == ["SECRET"]
    assert decision.redacted_text.startswith("key:\n<SECRET_1>")
    assert BODY not in decision.redacted_text
    assert "PRIVATE KEY" not in decision.redacted_text


@pytest.mark.parametrize(
    "token",
    (
        "rk_live_0123456789abcdef0123",
        "rk_test_0123456789abcdef0123",
        "x" + "app-1-A0123456789-0123456789-0123456789abcdef",
        "gl" + "rt-0123456789abcdefABCD",
        "gl" + "dt-0123456789abcdefABCD",
        "gl" + "ptt-0123456789abcdefABCD",
        "gl" + "ft-0123456789abcdefABCD",
        "gl" + "soat-0123456789abcdefABCD",
    ),
)
def test_a_bare_vendor_token_is_masked(token: str) -> None:
    decision = evaluate(f"use {token} here")

    assert decision.redacted_text == "use <SECRET_1> here"


# `rk_` ends English words (benchmark, network, framework). An `sk_` key glued
# to a word stays read as 1.2.2 read it, `Task_live_...` included: no rule on
# the text tells that identifier from a key glued to a label.
@pytest.mark.parametrize(
    "text",
    (
        "def benchmark_test_ParallelExecution():",
        "network_test_ConnectionTimeout1",
        "def test_framework_test_ConfigurationLoader():",
    ),
)
def test_a_code_identifier_ending_in_a_vendor_prefix_is_no_token(text: str) -> None:
    assert evaluate(text).redacted_text == text


@pytest.mark.parametrize(
    "separator", (":", "=", "_", '"', " ", "'", "1", "x1", r"\n", r"\t")
)
def test_a_stripe_key_after_a_separator_is_masked(separator: str) -> None:
    token = "rk_live_0123456789abcdef0123"

    assert evaluate(f"x{separator}{token}").redacted_text == f"x{separator}<SECRET_1>"


@pytest.mark.parametrize(
    "before", ("STRIPE_SECRET", "stripe:secret", "Bearer", "key", "abc123", "1", "x")
)
def test_a_stripe_secret_key_glued_to_text_is_masked(before: str) -> None:
    text = f"{before}sk_live_51AbCdEfGhIjKlMnOpQr here"

    assert evaluate(text).redacted_text == f"{before}<SECRET_1> here"


# The 1.2.2 Stripe rule, frozen: a key it read must stay masked in any context.
_STRIPE_1_2_2 = re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{16,}")
_CONTEXTS = (
    *(chr(code) for code in range(32, 127)),
    *(rf"\{letter}" for letter in "nrtfbv"),
    "",
    "STRIPE_SECRET",
    "abc123",
    "Task",
    "é",
    "​",
    "ﬁ",
)


@pytest.mark.parametrize(
    "key",
    (
        "sk_live_51AbCdEfGhIjKlMnOpQr",
        "sk_test_SyntheticKeyBodyOnlyLetters",
        "sk_" + "live_012345678901234567890123",
    ),
)
def test_every_stripe_key_1_2_2_read_is_still_masked(key: str) -> None:
    body = key[8:]
    missed = [
        (before, after)
        for before in _CONTEXTS
        for after in ("", " ", "x", "\n")
        if _STRIPE_1_2_2.search(text := f"{before}{key}{after}")
        and body in evaluate(text).redacted_text
    ] + [
        ("", after)
        for after in _CONTEXTS
        if body in evaluate(f"{key}{after}").redacted_text
    ]

    assert missed == []


@pytest.mark.parametrize(
    "token",
    (
        "sk_live_51AbCdEfGhIjKlMnOpQr",
        "xoxb-0123456789-abcdefghij",
        "AIzaSyA0123456789abcdefghijklmnopqrstuv",
        "glpat-0123456789abcdefghij",
        "npm_0123456789abcdefghijklmnopqrstuvwxyz",
        "hf_0123456789abcdefghijklmnopqrstuv",
    ),
)
@pytest.mark.parametrize("escape", (r"\n", r"\t", r"\r"))
def test_a_vendor_token_after_an_escape_written_as_text_is_masked(
    token: str, escape: str
) -> None:
    text = f'{{"env": "A=1{escape}{token}"}}'

    assert evaluate(text).redacted_text == f'{{"env": "A=1{escape}<SECRET_1>"}}'


def test_a_proxy_authorization_header_is_masked_like_authorization() -> None:
    decision = evaluate(
        "Proxy-Authorization: Basic c3ludGhldGljOjAxMjM0NTY3ODlhYmNkZWY="
    )

    assert decision.redacted_text == "Proxy-Authorization: Basic <SECRET_1>"


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("SSN 536221234", "SSN <US_SSN_1>"),
        ("Social Security Number 536221234", "Social Security Number <US_SSN_1>"),
        ('{"ssn": "536904399"}', '{"ssn": "<US_SSN_1>"}'),
        (
            '{"social_security_number": 536904399}',
            '{"social_security_number": <US_SSN_1>}',
        ),
        ("userSsn=536904399", "userSsn=<US_SSN_1>"),
        ('"ssn" : "536221234"', '"ssn" : "<US_SSN_1>"'),
        ('{ "ssn"  :  "536221234" }', '{ "ssn"  :  "<US_SSN_1>" }'),
        ("SSNs: 536221234", "SSNs: <US_SSN_1>"),
        (
            "SSNs: 536221234, 536904399",
            "SSNs: <US_SSN_1>, <US_SSN_2>",
        ),
        # Only an SSN it follows makes the next run one; other words break the list.
        (
            "SSN 536221234 and order 536904399",
            "SSN <US_SSN_1> and order 536904399",
        ),
    ),
)
def test_a_bare_ssn_after_an_ssn_cue_is_masked(text: str, expected: str) -> None:
    assert evaluate(text).redacted_text == expected


@pytest.mark.parametrize(
    "text",
    (
        "order 536221234 shipped",
        "SSN 123456789 filed",
        "SSN 078051120 filed",
        "SSN 666221234 filed",
        "SSN 536001234 filed",
        "SSN 5362212345 filed",
        "lessn 536221234",
        "SSN filed on 2026-10-19 by clerk 7, batch 536221234",
    ),
)
def test_a_bare_nine_digit_run_without_a_valid_cued_ssn_stays(text: str) -> None:
    # A ten-digit run may still be a phone; it is never an SSN.
    assert "US_SSN" not in {f.entity_type for f in evaluate(text).findings}


@pytest.mark.parametrize(
    "card",
    (
        "4111111111111111110",
        "411111111111111118",
        "6212345678901234569",
        "4111 1111 1111 1111 110",
    ),
)
def test_a_luhn_valid_eighteen_or_nineteen_digit_card_is_masked(card: str) -> None:
    decision = evaluate(f'{{"pan": "{card}"}}')

    assert decision.redacted_text == '{"pan": "<CREDIT_CARD_1>"}'


@pytest.mark.parametrize(
    "text",
    ("id 4111111111111111111", "id 411111111111111111", "id 1234567890123456789"),
)
def test_a_long_digit_run_that_fails_luhn_is_no_card(text: str) -> None:
    assert evaluate(text).findings == ()


def test_a_sixteen_digit_card_before_a_short_number_is_still_masked() -> None:
    decision = evaluate("Card 4111 1111 1111 1111 12 items")

    assert decision.redacted_text == "Card <CREDIT_CARD_1> 12 items"


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        # The input of the deleted test_malformed_percent_encoded_utf8_fails_safely.
        ("alice%C3%28@example.com", "<EMAIL_1>"),
        ("Contact jane.doe%C3%28@example.com about it", "Contact <EMAIL_1> about it"),
        ("x jane.doe%FF@example.com", "x <EMAIL_1>"),
    ),
)
def test_an_address_as_written_is_masked_when_decoding_breaks_it(
    text: str, expected: str
) -> None:
    assert evaluate(text).redacted_text == expected


# The text as written is read too, but a reference is one once decoded.
@pytest.mark.parametrize(
    "text",
    (
        "amqp://app:%24%7BAMQP_PASS%7D@rabbit.internal//",
        "ftp://%24%7BUSER%7D:%24%7BPASS%7D@h/",
        "postgres://%24%7BU%7D:%24%7BP%7D@h/db",
        "smtp://u:%2A%2A%2A@mail/",
        "password=%24%7BDB_PASS%7D",
        'secret: "%24%7BS%7D"',
        "token=%24TOKEN",
        "password=%3Cyour-password%3E",
        "password=%3CSECRET_1%3E",
    ),
)
def test_a_reference_written_with_percent_escapes_is_no_secret(text: str) -> None:
    assert evaluate(text).findings == ()


def test_a_value_that_is_a_reference_only_after_two_decodings_is_masked() -> None:
    decision = evaluate("password=%2524%257BX%257D")

    assert decision.redacted_text == "password=<SECRET_1>"


def test_the_text_as_written_and_the_decoded_text_are_both_read() -> None:
    decision = evaluate("a alice%C3%28@example.com b bob%40example.org c")

    assert decision.redacted_text == "a <EMAIL_1> b <EMAIL_2> c"


PASSWORD = "Synth3ticPassw0rd"


def _every_escape(text: str) -> str:
    return "".join(f"%{ord(character):02X}" for character in text)


# Encoded twice, the key too: the decoded reading holds `password=%24Synth...`,
# which is a password, not the reference `$Synth...` it decodes to again.
_SHAPES = (
    f"${PASSWORD}",
    f"${{{PASSWORD}}}",
    f"%{PASSWORD}%",
    f"env({PASSWORD}",
    f"getenv({PASSWORD}",
    f"process.env.{PASSWORD}",
    f"os.environ[{PASSWORD}",
    f"Optional[{PASSWORD}",
)


# Escaping only symbols leaves `process.env.` and `os.environ` readable after one
# decoding, where they are references, as on the tip and in 1.2.2.
@pytest.mark.parametrize(
    ("value", "every"),
    [
        (value, every)
        for value in _SHAPES
        for every in (False, True)
        if every or not value.startswith(("process.env.", "os.environ["))
    ],
)
def test_a_password_encoded_twice_is_not_read_as_a_reference(
    value: str, every: bool
) -> None:
    once = _every_escape(value) if every else quote(value, safe="")
    secret = quote(_every_escape(PASSWORD), safe="") if every else PASSWORD

    redacted = evaluate(f"x password%3D{quote(once, safe='')} y").redacted_text

    assert secret not in redacted


@pytest.mark.parametrize(
    "text",
    (
        "GET /login?next=%2Fcallback%3Fpassword%3D%2524Synth3ticPassw0rd 302",
        "redirect_uri=https%3A%2F%2Fapp.example.com%2Fcb%3Faccess_token%3D"
        "%2524Synth3ticPassw0rd",
    ),
)
def test_a_password_encoded_twice_in_a_url_is_masked(text: str) -> None:
    assert PASSWORD not in evaluate(text).redacted_text


def test_a_password_that_decodes_past_the_input_limit_is_still_read(
    tmp_path,
) -> None:
    # U+FDFA decodes to 18 characters: the password alone would pass the limit.
    code = (
        "from shim_cli.guard import evaluate\n"
        "text = 'see https://user:' + '\\ufdfa' * 6000 + "
        "'Synth3ticPassw0rd@host.example.com/x ok alice@example.com'\n"
        "print(sorted({f.entity_type for f in evaluate(text).findings}))\n"
    )

    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", code),
        capture_output=True,
        env={"HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
        check=True,
        timeout=60,
    )

    assert result.stdout.decode().split() == ["['EMAIL',", "'SECRET']"]


def _custom(pattern: str, ignore_case: bool = False):
    entry = {"name": "P", "pattern": pattern, "ignore_case": ignore_case}
    return compile_custom([entry])[0].regex


@pytest.mark.parametrize(
    "pattern",
    (
        r"\d+-\d+-\d+",
        r"(\d+\s?)+x",
        r"[0-9]+[0-9]+;",
        r".*@corp\.internal",
        r"\s*\s*end",
        # Quadratic only past a counted run longer than the early probes.
        r"\d{1025}\d*\d*;",
        r"1{1100}1*1*;",
        r"(?<=1{1100})1*1*;",
        r"(?:1a){600}(?:1a)*(?:1a)*;",
        r"1{5000}1*1*;",
    ),
)
def test_a_quadratic_pattern_is_refused(pattern: str) -> None:
    regex = _custom(pattern)

    started = time.perf_counter()
    reason = unsafe_pattern("P", regex.pattern, regex.flags)

    assert reason == "pattern P backtracks on repeated input; simplify it"
    assert time.perf_counter() - started < 5


def test_a_pattern_is_checked_with_the_flags_the_hook_compiles_it_with() -> None:
    pattern = r"\b(?:id|ID)*;"
    sensitive, insensitive = _custom(pattern), _custom(pattern, True)

    assert unsafe_pattern("P", sensitive.pattern, sensitive.flags) == ""
    started = time.perf_counter()
    reason = unsafe_pattern("P", insensitive.pattern, insensitive.flags)

    assert reason == "pattern P backtracks on repeated input; simplify it"
    # The probe grows, so an exponential pattern is caught near the limit,
    # not after it has run for minutes.
    assert time.perf_counter() - started < 20 * PROBE_MILLISECONDS / 1000


@pytest.mark.parametrize(
    "pattern",
    (
        r"\bATLAS-[0-9]{4}\b",
        r"\b[a-z]+\.corp\.internal\b",
        "[a-z.@]+",
        r"PROJ-1\.2\+3",
        r"(?<!\w)atlas(?!\w)",
        r"\bTICKET-\d{3,8}\b",
        r"[A-Z]{2}-\d+",
    ),
)
def test_a_linear_pattern_is_accepted(pattern: str) -> None:
    for ignore_case in (False, True):
        regex = _custom(pattern, ignore_case)
        assert unsafe_pattern("P", regex.pattern, regex.flags) == ""


def test_a_long_linear_pattern_is_checked_quickly() -> None:
    # Many characters make many runs to try; doctor checks up to 32 patterns.
    pattern = (
        r"\b(?:ACME-[0-9]{4}|PROJ_[A-Z]{3}|ticket#\d{2,6}|order/(?:eu|us|tr)-\d{5}"
        r"|inv\.\d{4}-[a-z]{2}|ref:[A-F0-9]{8}|cust@[a-z]{3,8}|ZETA~\d{3}"
        r"|kappa=\d{2}|omega;[xyz]{2}|q!\d{3}|w\$\d{2})\b"
    )
    regex = _custom(pattern, True)

    started = time.perf_counter()
    reason = unsafe_pattern("P", regex.pattern, regex.flags)

    assert reason == ""
    assert time.perf_counter() - started < 0.8
