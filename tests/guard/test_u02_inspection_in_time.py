"""The detector reads every input the hook accepts, up to 1 MB, inside the
hook's deadline, and finds what the ported recognisers found."""

from __future__ import annotations

import importlib
import random
import re
import subprocess
import sys
import time
import unicodedata

import pytest

from shim_cli.guard import evaluate
from shim_cli.guard.normalize import InputTooLarge, normalize

_OWNER = "owner,ops@example.com,AKIAIOSFODNN7EXAMPLE\n"
_MEGABYTE = 1_000_000


def _megabyte(unit) -> str:
    """`_OWNER`, then `unit(row)` for row 0, 1, ... up to the hook's 1 MB."""
    rows, size, row = [_OWNER], len(_OWNER), 0
    while True:
        text = unit(row)
        size += len(text.encode())
        if size > _MEGABYTE:
            return "".join(rows)
        rows.append(text)
        row += 1


def _digits(row: int, count: int) -> str:
    return str(row * 7_919 + 1_000_003)[-count:].zfill(count)


@pytest.mark.parametrize(
    "unit",
    [
        lambda row: f"2024-03-07,{_digits(row, 6)},{_digits(row, 4)}.12,555{row}\n",
        lambda row: f"{row % 223 + 1}.{row * 91 % 256}.{row * 53 % 256}.{row % 256}\n",
        lambda row: "ZXlK",
        lambda row: "a." * 2_000 + "@ ",
        lambda row: "ﷺ",
        lambda row: f"({row % 800 + 200}) 555-{_digits(row, 4)}\n",
        lambda row: f"({row % 800 + 200}) 555-{_digits(row, 4)} ext. {row % 1000}\n",
        lambda row: "\u0301" * 50_000 + "\u0316" * 50_000 + "\n",
    ],
    ids=[
        "numeric-csv",
        "address-lines",
        "base64-of-jwt-prefixes",
        "email-punctuation",
        "expanding-unicode",
        "phone-list",
        "phone-list-with-extensions",
        "combining-marks",
    ],
)
def test_a_megabyte_of_worst_case_text_is_inspected_whole_in_time(unit) -> None:
    """The hook takes up to 1 MB and stops at 25 s. A 650 KB numeric CSV, a
    960 KB phone list, the same list with extensions, 990 KB of U+FDFA, which
    NFKD writes as 18 characters, and runs of combining marks, which NFC
    orders by insertion sort, all used to reach the deadline and pass through
    whole, the key on row 1 included. The bound is the detector's own CPU time, so a busy machine does
    not fail it, and half the deadline leaves the rest for the event around it
    and for a slower machine."""
    hook = importlib.import_module("shim_cli.hook")
    text = _megabyte(unit)

    started = time.process_time()
    decision = evaluate(text)
    spent = time.process_time() - started

    assert decision.partial is False
    assert decision.redacted_text.startswith("owner,<EMAIL_1>,<SECRET_1>\n")
    assert spent < hook.HOOK_DEADLINE_SECONDS / 2


def test_a_run_of_marks_is_normalised_whole_up_to_thirty() -> None:
    """NFC orders a run of combining marks by insertion sort, in one call the
    hook's deadline cannot interrupt. A run is cut after 30 marks, as Unicode's
    stream-safe text format does; a shorter run normalises as before."""
    short = "a" + "\u0301\u0316" * 15
    long = "a" + "\u0301" * 30 + "\u0316"

    assert normalize(short).text == unicodedata.normalize("NFC", short)
    assert normalize(long).text == (
        unicodedata.normalize("NFC", "a" + "\u0301" * 30) + "\u0316"
    )


def test_text_that_expands_past_the_limit_still_has_its_secret_masked() -> None:
    """NFKD turns U+FDFA into 18 characters: 12,000 of them in a 20 KB field
    normalise past the analysis limit, and the field used to be refused whole,
    with the key beside them."""
    text = "ﷺ" * 12_000 + " AKIAIOSFODNN7EXAMPLE " + "x" * 8_000

    decision = evaluate(text)

    assert decision.partial is False
    assert decision.counts == (("SECRET", 1),)
    assert "AKIAIOSFODNN7EXAMPLE" not in decision.redacted_text
    assert decision.redacted_text.startswith("ﷺ" * 12_000 + " <SECRET_1> ")


@pytest.mark.parametrize(
    ("text", "escape"),
    [
        ("key AKIAIOSFODNN7EXAMPLE " + "ﷺ" * 20_000, 1),
        ("ﷺ" * 5_000 + "%EF%B7%BA" * 7_000, len("%EF%B7%BA")),
    ],
    ids=["written", "percent-encoded"],
)
def test_normalisation_says_how_much_of_a_text_fits(text: str, escape: int) -> None:
    """Exactly the source characters before the first that does not fit; a
    percent escape fits whole or not at all."""
    with pytest.raises(InputTooLarge) as refused:
        normalize(text)
    fits = refused.value.fits

    assert 0 < fits < len(text)
    assert normalize(text[:fits]).source_spans[-1][1] == fits
    with pytest.raises(InputTooLarge):
        normalize(text[: fits + escape])


def test_a_field_that_expands_past_the_limit_is_cut_once_where_it_stops_fitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The field used to be halved, and each half refused again, down to a
    smallest piece: 100,000 characters of U+FDFA were refused 15 times and
    analysed 1.94 times over. Now one refusal says where the text stops fitting,
    and only the 4,096 characters each cut reads twice are read again."""
    evaluate_module = sys.modules["shim_cli.guard.evaluate"]
    real = evaluate_module.analyze_counting
    analysed: list[int] = []
    refused: list[int] = []

    def counted(text, entities=(), custom=()):
        try:
            found = real(text, entities, custom)
        except InputTooLarge:
            refused.append(len(text))
            raise
        analysed.append(len(text))
        return found

    monkeypatch.setattr(evaluate_module, "analyze_counting", counted)
    text = "ﷺ" * 99_000 + " AKIAIOSFODNN7EXAMPLE"

    decision = evaluate(text)

    assert decision.partial is False
    assert decision.redacted_text.endswith(" <SECRET_1>")
    assert refused == [len(text)]
    # Every piece but the last is as long as fits, and starts 4,096 back.
    *full, last = analysed
    assert set(full) == {full[0]} and last <= full[0]
    assert sum(analysed) - 4_096 * len(full) == len(text)


_REFERENCE_VENDOR_TOKEN = re.compile(
    r"(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"sk_(?:live|test)_[A-Za-z0-9]{16,}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
    r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|"
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"(?<![\w-])(?:xox[abposr]-[0-9A-Za-z-]{10,}|AIza[0-9A-Za-z_-]{35}|"
    r"github_pat_[0-9A-Za-z_]{22,}|glpat-[0-9A-Za-z_-]{20,}|"
    r"npm_[0-9A-Za-z]{36}|hf_[0-9A-Za-z]{30,})(?![\w-]))"
)
_TOKEN_PARTS = (
    "eyJ",
    "eyJ",
    "a",
    "b",
    "9",
    ".",
    ".",
    "-",
    "_",
    " ",
    "x",
    "AKIAIOSFODNN7EXAMPLE",
    "AKIA",
    "sk-" + "a1" * 8,
    "ghp_" + "b2" * 10,
    "xoxb-" + "1" * 10,
    "SG." + "c" * 16 + "." + "d" * 16,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
)


def test_the_token_scan_finds_what_the_ported_pattern_found() -> None:
    """The JWT alternative left the vendor pattern because trying every `eyJ`
    of `eyJeyJ...` was quadratic. The scan that replaced it went on from inside
    a token it had matched, so `eyJa.eyJb.c.d` also masked `.d`, and a phone
    after a JWT became part of the secret. It must find the ported pattern's
    spans, no more and no fewer."""
    from shim_cli.guard.recognizers import _vendor_tokens

    pick = random.Random(23)
    texts = ["eyJa.eyJb.c.d", "eyJa.eyJb.c.5551234567", "x eyJa.eyJb.c.eyJd.e.f"]
    texts += [
        "".join(pick.choice(_TOKEN_PARTS) for _ in range(pick.randrange(1, 30)))
        for _ in range(20_000)
    ]

    for text in texts:
        expected = [found.span() for found in _REFERENCE_VENDOR_TOKEN.finditer(text)]
        assert list(_vendor_tokens(text)) == expected, text


@pytest.mark.parametrize("prefix", ["٣", "2٣", "२"])
def test_an_address_with_a_prefix_in_other_digits_is_left_as_it_was(
    prefix: str,
) -> None:
    """`ipaddress` refuses a prefix written in other digits, so the address was
    never reported; the quicker check for dotted addresses must not start
    masking it."""
    text = f"host 10.0.0.1/{prefix} up"

    assert evaluate(text, ("IP_ADDRESS",)).redacted_text == text


_PHONE_SHAPES = (
    "(212) 555-0143",
    "212-555-0143",
    "212–555–0143",
    "(212) 555-0143 / (646) 555-0199",
    "2125550143",
    "+1 416 555 0199",
    "1-800-555-0199",
    "020 7946 0958",
    "07700 900123",
    "+44 20 7946 0958",
    "00 44 20 7946 0958",
    "011 44 20 7946 0958",
    "030 1234567",
    "0151 23456789",
    "+49 30 1234567",
    "0049 30 1234567",
    "01 23 45 67 89",
    "06 12 34 56 78",
    "03-1234567",
    "050-1234567",
    "+972-3-1234567",
    "098765 43210",
    "+91 98765 43210",
    "011 2345 6789",
    "(11) 91234-5678",
    "11 3456-7890",
    "0532 123 45 67",
    "(0212) 555 01 43",
    "+90 532 123 4567",
    "905321234567",
    "5321234567",
    "310-1234",
    "444 1 444",
    "4004-1234",
    "16412345",
    "0180 1 2345",
    "030 123456",
    "02-123-4567",
    "651-234-2345/332-445-1234",
    "650-253-0000 ext. 123",
    "650 253 0000 x12",
    "(212) 555-0143 ext. 7",
    "212-555-0143 ext",
    "(212) 555-0143 x1x2",
    "5551234567~123",
    "212-555-0143 - 503#",
    "+1 650 253 0000 ext 9",
    "+44 20 7946 0958 #12",
    "030 1234567;ext=12",
    "06 12 34 56 78,,123",
    "0532 123 45 67 x 12",
    "0532 123 45 67 int 5",
    "1-800-555-0199 anexo 12",
    "+90 532 123 4567 доб 3",
    "٠٥٣٢ ١٢٣ ٤٥ ٦٧ x ١٢",
    "123~45",
    "abc8005001234",
    "8005001234def",
    "3/10/2011",
    "2012-01-02 08:00",
    "211-227 (2003)",
    "+0532 123 45 67",
    "+1+1+1",
    "٠٥٣٢ ١٢٣ ٤٥ ٦٧",
    "%2125550143",
    "$2125550143",
)
_PHONE_NOISE = (
    "2024-01-31,123456,7890.12,555123",
    "93.86.157.128",
    "10.0.0.1",
    "2026-10-17T10:00:01.123Z 200 1234",
    "1757496600",
    "v1.2.3.4",
    "1 1 1 1 1 1",
    "id 4",
    "tel:",
    "call",
    "no.",
    "(",
    ")",
    " - ",
    "/",
    ".",
)


def test_the_phone_scan_finds_exactly_what_each_region_s_matcher_finds() -> None:
    """The scan walks the nine regions together and calls the parser only
    where a region could accept the number. Its spans must be the library's
    own: nine `PhoneNumberMatcher`s, one per region, over the same text."""
    import phonenumbers

    from shim_cli.guard.recognizers import (
        _FEWEST_PHONE_DIGITS,
        _PHONE_REGIONS,
        _phone_spans,
    )

    # The scan skips tries with fewer digits than any region's shortest number.
    assert _FEWEST_PHONE_DIGITS <= min(
        min(metadata.general_desc.possible_length)
        for code, names in phonenumbers.COUNTRY_CODE_TO_REGION_CODE.items()
        for name in names
        for metadata in [
            phonenumbers.PhoneMetadata.metadata_for_region_or_calling_code(code, name)
        ]
    )

    pick = random.Random(19)
    parts = []
    for _ in range(1_500):
        parts.append(pick.choice(_PHONE_SHAPES + _PHONE_NOISE))
        parts.append(pick.choice((" ", " ", ", ", "; ", "\n", "\r\n", " tel ", "")))
    text = "".join(parts)
    expected = {
        (found.start, found.end)
        for region in _PHONE_REGIONS
        for found in phonenumbers.PhoneNumberMatcher(text, region, leniency=1)
    }

    assert len(expected) > 300
    assert _phone_spans(text) == expected


def test_a_region_s_numbering_plan_is_read_only_for_a_candidate(tmp_path) -> None:
    """As in 1.2.1, digits too few for any number read no region's metadata.
    Reading all nine regions' (35 modules, for North America's shared code)
    whenever a digit appeared cost 13 to 28 ms a process: a fifth of the hook
    on a 1 KB prompt. A fresh process, so no other test has read them."""
    code = (
        "import sys\n"
        "from shim_cli.guard import evaluate\n"
        "for text in sys.argv[1:]:\n"
        "    evaluate(text)\n"
        "    print(sum(name.startswith('phonenumbers.data.region_')"
        " for name in sys.modules))\n"
    )
    texts = ("version 3.13, line 42, key AKIAIOSFODNN7EXAMPLE", "call 0532 123 45 67")

    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", code, *texts),
        capture_output=True,
        env={"HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
        check=True,
        timeout=60,
    )

    digit_light, candidate = map(int, result.stdout.split())
    assert digit_light == 0
    assert candidate > 0


@pytest.mark.parametrize("extension", ["x{}", " ~{}", " #{}"])
def test_an_extension_costs_the_parser_nothing_more(
    monkeypatch: pytest.MonkeyPatch, extension: str
) -> None:
    """The parser reads a number without its extension, so the regions'
    metadata rules out the same numbers with an extension as without one. It
    used to send every number with an extension to all nine regions' parsers:
    1 MB of them took 20 to 40 s."""
    from phonenumbers import phonenumbermatcher

    from shim_cli.guard.recognizers import _phone_spans, _phone_walk

    # The walk remembers candidates it has read: start with none, so the
    # parser is asked about every row of both lists.
    _phone_walk.cache_clear()
    real = phonenumbermatcher.PhoneNumberMatcher._parse_and_verify
    asked: list[str] = []

    def counted(matcher, candidate, offset):
        asked.append(candidate)
        return real(matcher, candidate, offset)

    monkeypatch.setattr(
        phonenumbermatcher.PhoneNumberMatcher, "_parse_and_verify", counted
    )
    rows = [f"({row % 800 + 200}) 555-{_digits(row, 4)}" for row in range(500)]

    plain = _phone_spans("\n".join(rows))
    plain_asked = len(asked)
    asked.clear()
    extended = _phone_spans(
        "\n".join(f"{text}{extension.format(row)}" for row, text in enumerate(rows))
    )

    assert len(asked) == plain_asked
    assert len(extended) == len(plain)
    asked.clear()
    assert _phone_spans("123~45 " * 2_000) == set()
    assert asked == []


def test_a_number_after_a_long_run_of_digits_is_still_read() -> None:
    """The library gives up after 65,535 failed tries; a normalised piece of
    200,000 characters of spaced digits hid the number at its end."""
    from shim_cli.guard.recognizers import _scan_phone

    text = "1 " * 99_000 + "tel 0532 123 45 67"

    assert _scan_phone(text)[-1][1:3] == (198_004, 198_018)
