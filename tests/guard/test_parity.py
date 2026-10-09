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

BARE_ID = (
    "The frozen output was the phone recogniser firing on a bare id; the "
    "corpus's own siblings of the same shape are frozen unmasked. A bare digit "
    "run is now a phone number only when Turkish-shaped or cued (PRD-21)."
)

USERINFO_ONLY = (
    "A connection string is masked over its user-info only, so the model keeps "
    "the scheme, host, port and database it needs to debug and loses the "
    "credentials (PRD-36)."
)
NO_CREDENTIAL = (
    "A connection string that carries no credentials names a host, not a "
    "secret, wherever it points (PRD-36 R3)."
)

DELIBERATE_DIVERGENCES = {
    "ssn-bare-78051120": (BARE_ID, []),
    "vkn-nocontext-123456789": (BARE_ID, []),
    "vkn-farcontext-123456789": (BARE_ID, []),
    "vkn-nocontext-222222222": (BARE_ID, []),
    "vkn-farcontext-222222222": (BARE_ID, []),
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
    "uri-no-credentials": (NO_CREDENTIAL, []),
}

# A divergence that still masks, but over fewer characters. Kept apart from
# DELIBERATE_DIVERGENCES because those assert nothing is masked at all.
NARROWED_SPANS = {
    "email-in-url": (
        "The local part no longer accepts `=`, so a query string stops being "
        "read as part of an address. The URL survives with only the address "
        "masked, instead of the host and path disappearing into the "
        "placeholder.",
        [["EMAIL", 28, 45, 1.0]],
        "See https://example.com/u?e=<EMAIL_1> now",
    ),
    "uri-postgres": (
        USERINFO_ONLY,
        [["DB_URI", 19, 33, 0.99]],
        "Connect postgres://<DB_URI_1>@db.example.com:5432/app now",
    ),
    "uri-trailing-postgres": (
        USERINFO_ONLY,
        [["DB_URI", 19, 33, 0.99]],
        "Connect postgres://<DB_URI_1>@db.example.com:5432/app.",
    ),
    "uri-quoted-postgres": (
        USERINFO_ONLY,
        [["DB_URI", 20, 34, 0.99]],
        'Connect "postgres://<DB_URI_1>@db.example.com:5432/app" now',
    ),
    "uri-paren-postgres": (
        USERINFO_ONLY,
        [["DB_URI", 20, 34, 0.99]],
        "Connect (postgres://<DB_URI_1>@db.example.com:5432/app)",
    ),
    "uri-postgresql": (
        USERINFO_ONLY,
        [["DB_URI", 21, 35, 0.99]],
        "Connect postgresql://<DB_URI_1>@db.example.com:5432/app now",
    ),
    "uri-trailing-postgresql": (
        USERINFO_ONLY,
        [["DB_URI", 21, 35, 0.99]],
        "Connect postgresql://<DB_URI_1>@db.example.com:5432/app.",
    ),
    "uri-quoted-postgresql": (
        USERINFO_ONLY,
        [["DB_URI", 22, 36, 0.99]],
        'Connect "postgresql://<DB_URI_1>@db.example.com:5432/app" now',
    ),
    "uri-paren-postgresql": (
        USERINFO_ONLY,
        [["DB_URI", 22, 36, 0.99]],
        "Connect (postgresql://<DB_URI_1>@db.example.com:5432/app)",
    ),
    "uri-mysql": (
        USERINFO_ONLY,
        [["DB_URI", 16, 30, 0.99]],
        "Connect mysql://<DB_URI_1>@db.example.com:5432/app now",
    ),
    "uri-trailing-mysql": (
        USERINFO_ONLY,
        [["DB_URI", 16, 30, 0.99]],
        "Connect mysql://<DB_URI_1>@db.example.com:5432/app.",
    ),
    "uri-quoted-mysql": (
        USERINFO_ONLY,
        [["DB_URI", 17, 31, 0.99]],
        'Connect "mysql://<DB_URI_1>@db.example.com:5432/app" now',
    ),
    "uri-paren-mysql": (
        USERINFO_ONLY,
        [["DB_URI", 17, 31, 0.99]],
        "Connect (mysql://<DB_URI_1>@db.example.com:5432/app)",
    ),
    "uri-mongodb": (
        USERINFO_ONLY,
        [["DB_URI", 18, 32, 0.99]],
        "Connect mongodb://<DB_URI_1>@db.example.com:5432/app now",
    ),
    "uri-trailing-mongodb": (
        USERINFO_ONLY,
        [["DB_URI", 18, 32, 0.99]],
        "Connect mongodb://<DB_URI_1>@db.example.com:5432/app.",
    ),
    "uri-quoted-mongodb": (
        USERINFO_ONLY,
        [["DB_URI", 19, 33, 0.99]],
        'Connect "mongodb://<DB_URI_1>@db.example.com:5432/app" now',
    ),
    "uri-paren-mongodb": (
        USERINFO_ONLY,
        [["DB_URI", 19, 33, 0.99]],
        "Connect (mongodb://<DB_URI_1>@db.example.com:5432/app)",
    ),
    "uri-mongodb+srv": (
        USERINFO_ONLY,
        [["DB_URI", 22, 36, 0.99]],
        "Connect mongodb+srv://<DB_URI_1>@db.example.com:5432/app now",
    ),
    "uri-trailing-mongodb+srv": (
        USERINFO_ONLY,
        [["DB_URI", 22, 36, 0.99]],
        "Connect mongodb+srv://<DB_URI_1>@db.example.com:5432/app.",
    ),
    "uri-quoted-mongodb+srv": (
        USERINFO_ONLY,
        [["DB_URI", 23, 37, 0.99]],
        'Connect "mongodb+srv://<DB_URI_1>@db.example.com:5432/app" now',
    ),
    "uri-paren-mongodb+srv": (
        USERINFO_ONLY,
        [["DB_URI", 23, 37, 0.99]],
        "Connect (mongodb+srv://<DB_URI_1>@db.example.com:5432/app)",
    ),
    "uri-redis": (
        USERINFO_ONLY,
        [["DB_URI", 16, 30, 0.99]],
        "Connect redis://<DB_URI_1>@db.example.com:5432/app now",
    ),
    "uri-trailing-redis": (
        USERINFO_ONLY,
        [["DB_URI", 16, 30, 0.99]],
        "Connect redis://<DB_URI_1>@db.example.com:5432/app.",
    ),
    "uri-quoted-redis": (
        USERINFO_ONLY,
        [["DB_URI", 17, 31, 0.99]],
        'Connect "redis://<DB_URI_1>@db.example.com:5432/app" now',
    ),
    "uri-paren-redis": (
        USERINFO_ONLY,
        [["DB_URI", 17, 31, 0.99]],
        "Connect (redis://<DB_URI_1>@db.example.com:5432/app)",
    ),
    "uri-rediss": (
        USERINFO_ONLY,
        [["DB_URI", 17, 31, 0.99]],
        "Connect rediss://<DB_URI_1>@db.example.com:5432/app now",
    ),
    "uri-trailing-rediss": (
        USERINFO_ONLY,
        [["DB_URI", 17, 31, 0.99]],
        "Connect rediss://<DB_URI_1>@db.example.com:5432/app.",
    ),
    "uri-quoted-rediss": (
        USERINFO_ONLY,
        [["DB_URI", 18, 32, 0.99]],
        'Connect "rediss://<DB_URI_1>@db.example.com:5432/app" now',
    ),
    "uri-paren-rediss": (
        USERINFO_ONLY,
        [["DB_URI", 18, 32, 0.99]],
        "Connect (rediss://<DB_URI_1>@db.example.com:5432/app)",
    ),
    "uri-mssql": (
        USERINFO_ONLY,
        [["DB_URI", 16, 30, 0.99]],
        "Connect mssql://<DB_URI_1>@db.example.com:5432/app now",
    ),
    "uri-trailing-mssql": (
        USERINFO_ONLY,
        [["DB_URI", 16, 30, 0.99]],
        "Connect mssql://<DB_URI_1>@db.example.com:5432/app.",
    ),
    "uri-quoted-mssql": (
        USERINFO_ONLY,
        [["DB_URI", 17, 31, 0.99]],
        'Connect "mssql://<DB_URI_1>@db.example.com:5432/app" now',
    ),
    "uri-paren-mssql": (
        USERINFO_ONLY,
        [["DB_URI", 17, 31, 0.99]],
        "Connect (mssql://<DB_URI_1>@db.example.com:5432/app)",
    ),
    "uri-embedded-secret": (
        USERINFO_ONLY,
        [["DB_URI", 13, 43, 0.99]],
        "postgresql://<DB_URI_1>@db.example.com/app.",
    ),
    "uri-upper-scheme": (
        USERINFO_ONLY,
        [["DB_URI", 21, 29, 0.99]],
        "Connect POSTGRESQL://<DB_URI_1>@db.example.com/app now",
    ),
    "overlap-uri-secret": (
        USERINFO_ONLY,
        [["DB_URI", 13, 43, 0.99]],
        "postgresql://<DB_URI_1>@db.example.com/app.",
    ),
    "overlap-card-in-uri": (
        USERINFO_ONLY,
        [["DB_URI", 13, 31, 0.99]],
        "postgresql://<DB_URI_1>@db.example.com/app",
    ),
}


def test_every_divergence_is_still_a_real_case() -> None:
    tables = (set(DELIBERATE_DIVERGENCES), set(NARROWED_SPANS), set(TIGHTENED))
    assert set().union(*tables) <= {case["id"] for case in _CASES}
    assert sum(map(len, tables)) == len(set().union(*tables))


def test_divergences_only_ever_relax_detection() -> None:
    by_id = {case["id"]: case for case in _CASES}
    for identifier, (_reason, expected) in DELIBERATE_DIVERGENCES.items():
        case = by_id[identifier]
        assert len(expected) < len(case["findings"]), identifier


def test_narrowed_spans_only_ever_shrink() -> None:
    """The mechanism must not become a way to quietly widen what shim masks."""
    by_id = {case["id"]: case for case in _CASES}
    for identifier, (_reason, expected, _redacted) in NARROWED_SPANS.items():
        frozen = by_id[identifier]["findings"]
        assert len(expected) == len(frozen), identifier
        for new_span, old_span in zip(expected, frozen, strict=True):
            assert new_span[0] == old_span[0], identifier
            assert new_span[1] >= old_span[1], identifier
            assert new_span[2] <= old_span[2], identifier
            assert new_span[1:3] != old_span[1:3], identifier


def test_the_frozen_corpus_is_substantial() -> None:
    assert _DOCUMENT["case_count"] == len(_CASES) >= 400
    assert sum(len(case["findings"]) for case in _CASES) >= 300
    assert len({case["id"] for case in _CASES}) == len(_CASES)


def test_the_generator_still_produces_the_frozen_inputs() -> None:
    generated = _generator().cases()
    assert [(identifier, text) for identifier, text in generated] == [
        (case["id"], case["text"]) for case in _CASES
    ]


LOWERCASE_IBAN = (
    "A lowercase IBAN with a valid checksum is an IBAN, as the gateway reads it "
    "(ShimIbanRecognizer at GetSHIM/shim ca6b2e9)."
)
WHOLE_IBAN = (
    "A spaced IBAN is one IBAN, never a card over its middle digits, as the "
    "gateway reads it."
)

# A ported gateway rule that masks more than the frozen build. Each new span
# covers every frozen one, so nothing that was masked comes back.
TIGHTENED = {
    **{
        f"iban-lower-{country}": (
            LOWERCASE_IBAN,
            [["IBAN", 5, end, 1.0]],
            "IBAN <IBAN_1> confirmed",
        )
        for country, end in (
            ("TR", 31),
            ("DE", 27),
            ("GB", 27),
            ("FR", 32),
            ("NL", 23),
            ("IT", 32),
            ("CH", 26),
            ("PL", 33),
            ("DK", 23),
            ("FI", 23),
            ("GR", 32),
            ("IE", 27),
            ("MT", 36),
            ("HR", 26),
            ("HU", 33),
            ("BG", 27),
            ("CY", 35),
            ("LI", 26),
            ("MC", 32),
            ("SM", 32),
        )
    },
    "iban-spaced-AT": (WHOLE_IBAN, [["IBAN", 5, 29, 1.0]], "IBAN <IBAN_1> confirmed"),
    "iban-spaced-PL": (WHOLE_IBAN, [["IBAN", 5, 39, 1.0]], "IBAN <IBAN_1> confirmed"),
}


def test_tightened_cases_never_unmask() -> None:
    by_id = {case["id"]: case for case in _CASES}
    for identifier, (_reason, expected, _redacted) in TIGHTENED.items():
        for _type, start, end, _score in by_id[identifier]["findings"]:
            assert any(
                new_start <= start and end <= new_end
                for _new, new_start, new_end, _ in expected
            ), identifier
        assert expected != by_id[identifier]["findings"], identifier


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
def test_detection_is_unchanged(case: dict) -> None:
    decision = evaluate(case["text"])
    actual = [
        [finding.entity_type, finding.start, finding.end, finding.score]
        for finding in decision.findings
    ]
    if case["id"] in NARROWED_SPANS:
        reason, expected, redacted = NARROWED_SPANS[case["id"]]
        assert actual == expected, f"{case['id']} narrows on purpose: {reason}"
        assert decision.redacted_text == redacted
        return
    if case["id"] in TIGHTENED:
        reason, expected, redacted = TIGHTENED[case["id"]]
        assert actual == expected, f"{case['id']} tightens on purpose: {reason}"
        assert decision.redacted_text == redacted
        return
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
