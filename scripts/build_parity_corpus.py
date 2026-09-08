"""Build the frozen detector-output refactoring oracle."""

from __future__ import annotations

import argparse
import json
import string
from pathlib import Path

CORPUS_VERSION = 1


def luhn(prefix: str, length: int) -> str:
    body = (prefix + "0123456789" * length)[: length - 1]
    total = 0
    for index, character in enumerate(reversed(body)):
        digit = int(character)
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return body + str((10 - total % 10) % 10)


def iban(country: str, body: str) -> str:
    rearranged = body + country + "00"
    digits = "".join(
        character
        if character in string.digits
        else str(10 + string.ascii_uppercase.index(character))
        for character in rearranged
    )
    check = 98 - int(digits) % 97
    return f"{country}{check:02d}{body}"


def tckn(seed: int) -> str:
    digits = [int(character) for character in f"{seed:09d}"]
    digits[0] = digits[0] or 1
    tenth = (sum(digits[0:9:2]) * 7 - sum(digits[1:8:2])) % 10
    digits.append(tenth)
    digits.append(sum(digits) % 10)
    return "".join(str(digit) for digit in digits)


def vkn(seed: int) -> str:
    digits = [int(character) for character in f"{seed:09d}"]
    checksum = 0
    for index, digit in enumerate(digits):
        adjusted = (digit + 9 - index) % 10
        if adjusted:
            checksum += (adjusted * 2 ** (9 - index)) % 9
    return "".join(str(digit) for digit in digits) + str((10 - checksum % 10) % 10)


_TLDS = (
    "com",
    "org",
    "net",
    "io",
    "dev",
    "app",
    "museum",
    "co.uk",
    "com.tr",
    "gov.uk",
    "blogspot.com",
    "invalid",
    "test",
    "example",
    "localhost",
    "internal",
    "lan",
    "local",
    "xn--p1ai",
    "рф",
    "COM",
    "Com",
    "c",
    "toolongtldthatdoesnotexistanywhere",
)
_IBAN_BODIES = (
    ("TR", "0006100519786457841326"),
    ("DE", "370400440532013000"),
    ("GB", "NWBK60161331926819"),
    ("FR", "20041010050500013M02606"),
    ("NL", "ABNA0417164300"),
    ("ES", "21000418450200051332"),
    ("IT", "X0542811101000000123456"),
    ("BE", "539007547034"),
    ("CH", "00762011623852957"),
    ("AT", "1904300234573201"),
    ("PL", "109010140000071219812874"),
    ("PT", "0002012319340100"),
    ("NO", "8601111794"),
    ("SE", "50000000058398257466"),
    ("DK", "00400440116243"),
    ("FI", "12345600000785"),
    ("GR", "01101250000000012300695"),
    ("IE", "AIBK93115212345678"),
    ("LU", "0019400644750000"),
    ("MT", "MALT011000012345MTLCAST001S"),
    ("RO", "AAAA1B31007593840000"),
    ("HR", "10010051863000160"),
    ("CZ", "08000000192000145399"),
    ("HU", "117730161111101800000000"),
    ("BG", "BNBG96611020345678"),
    ("EE", "2200221020145685"),
    ("CY", "17002001280000001200527600"),
    ("IS", "140346101027121203089"),
    ("LI", "088100002324013AA"),
    ("MC", "11222000010123456789030"),
    ("SM", "U0322509800000000270100"),
    ("AD", "12000120203100100"),
)
_SECRET_VALUES = (
    "AKIAIOSFODNN7EXAMPLE",
    "ghp_0123456789abcdefghijklmnopqrstuvwx",
    "gho_0123456789abcdefghijklmnopqrstuvwx",
    "ghs_0123456789abcdefghijklmnopqrstuvwx",
    "sk_live_0123456789abcdefgh",
    "sk_test_0123456789abcdefgh",
    "sk-0123456789abcdefghij",
    "sk-proj-0123456789abcdefghij",
    "SG.0123456789abcdefgh.0123456789abcdefgh",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij",
    "https://hooks.slack.com/services/T000/B000/XXXXXXXX",
    "https://discord.com/api/webhooks/123/abcdefgh",
    "https://discordapp.com/api/webhooks/123/abcdefgh",
)
_SECRET_KEYS = (
    "password",
    "passwd",
    "pwd",
    "api_key",
    "api-key",
    "apikey",
    "secret",
    "token",
    "db_pass",
    "db-pass",
    "postgres_password",
)
_DB_SCHEMES = (
    "postgres",
    "postgresql",
    "mysql",
    "mongodb",
    "mongodb+srv",
    "redis",
    "rediss",
    "mssql",
)
_PROSE_NEGATIVES = (
    "A password should never be shared in prose",
    "Rotate the token and password regularly",
    "The secret to good code is small functions",
    "Please store your api key somewhere safe",
    "This token represents a session",
    "Our secret sauce is boring engineering",
    "Set the password policy in the admin console",
    "The postgres adapter has no configured URI",
)


def _email_cases() -> list[tuple[str, str]]:
    cases = [
        (f"email-tld-{tld}", f"Contact alice@example.{tld} today") for tld in _TLDS
    ]
    cases += [
        ("email-subdomain", "Contact alice@mail.corp.example.com today"),
        ("email-plus", "Contact alice+tag@example.com today"),
        ("email-dots", "Contact a.l.i.c.e@example.com today"),
        ("email-dash-domain", "Contact alice@my-corp.example.com today"),
        ("email-upper", "Contact ALICE@EXAMPLE.COM today"),
        ("email-numeric-host", "Contact alice@192.168.1.1 today"),
        ("email-no-tld", "Contact alice@localhost today"),
        ("email-trailing-dot", "Contact alice@example.com. today"),
        ("email-two", "Contact alice@example.com and bob@example.org"),
        ("email-quoted", 'Contact "alice@example.com" today'),
        ("email-in-url", "See https://example.com/u?e=alice@example.com now"),
        ("email-punycode", "Contact alice@example.xn--p1ai today"),
        ("email-underscore", "Contact first_last@example.com today"),
        ("email-bang", "Contact alice!x@example.com today"),
    ]
    return cases


def _phone_cases() -> list[tuple[str, str]]:
    numbers = (
        "+90 532 123 45 67",
        "+905321234567",
        "+1 415 555 2671",
        "+44 20 7946 0958",
        "+49 30 901820",
        "+33 1 42 68 53 00",
        "+972 2 629 4444",
        "+91 22 2278 3000",
        "+55 11 4004 4828",
        "0532 123 45 67",
        "(415) 555-2671",
        "415-555-2671",
        "+34 91 123 45 67",
        "+81 3 3224 9999",
        "+90 532 000 00 00",
    )
    cases = [
        (f"phone-{index}", f"Phone {number} please")
        for index, number in enumerate(numbers)
    ]
    cases += [
        ("phone-negative-version", "Version 1.2.3 build 4567"),
        ("phone-negative-range", "Range 1000 to 2000 inclusive"),
        ("phone-negative-year", "Between 1999 and 2026 nothing happened"),
    ]
    return cases


def _card_cases() -> list[tuple[str, str]]:
    cases = []
    for prefix in ("4", "51", "6011", "1", "3", "5555", "4111"):
        for length in (13, 15, 16, 19):
            number = luhn(prefix, length)
            cases.append((f"card-{prefix}-{length}", f"Card {number} on file"))
            spaced = " ".join(
                number[index : index + 4] for index in range(0, len(number), 4)
            )
            cases.append((f"card-spaced-{prefix}-{length}", f"Card {spaced} on file"))
            hyphened = "-".join(
                number[index : index + 4] for index in range(0, len(number), 4)
            )
            cases.append((f"card-hyphen-{prefix}-{length}", f"Card {hyphened} on file"))
    cases += [
        ("card-invalid-luhn", "Invalid card 4111 1111 1111 1112"),
        ("card-order-number", "Order 1234567890123456 shipped"),
        ("card-isbn", "ISBN 9780306406157 is a book"),
    ]
    return cases


def _iban_cases() -> list[tuple[str, str]]:
    cases = []
    for country, body in _IBAN_BODIES:
        value = iban(country, body)
        cases.append((f"iban-{country}", f"IBAN {value} confirmed"))
        cases.append((f"iban-lower-{country}", f"IBAN {value.lower()} confirmed"))
        spaced = " ".join(value[index : index + 4] for index in range(0, len(value), 4))
        cases.append((f"iban-spaced-{country}", f"IBAN {spaced} confirmed"))
    cases += [
        ("iban-invalid", "Invalid IBAN TR000006100519786457841326"),
        ("iban-trailing-digit", "IBAN DE89370400440532013000 2 confirmed"),
        ("iban-in-sentence", "Send it to TR330006100519786457841326, thanks."),
    ]
    return cases


def _network_cases() -> list[tuple[str, str]]:
    values = (
        "192.168.1.1",
        "10.0.0.255",
        "255.255.255.255",
        "0.0.0.0",
        "192.168.1.1/24",
        "999.168.1.1",
        "256.1.1.1",
        "1.2.3",
        "2001:0db8:85a3:0000:0000:8a2e:0370:7334",
        "2001:db8::8a2e:370:7334",
        "::1",
        "::",
        "::ffff:192.168.1.1",
        "fe80::1%eth0",
        "00:1A:2B:3C:4D:5E",
        "00-1A-2B-3C-4D-5E",
        "001a.2b3c.4d5e",
        "00:1A:2B:3C:4D:GG",
        "FF:FF:FF:FF:FF:FF",
        "00:00:00:00:00:00",
        "00:1a:2b:3c:4d:5e",
    )
    return [
        (f"net-{index}", f"Host {value} responds") for index, value in enumerate(values)
    ]


def _identity_cases() -> list[tuple[str, str]]:
    cases = []
    for seed in (219099999, 123456789, 987654320, 78051120, 111223333):
        raw = f"{seed:09d}"
        for separator in ("-", " ", "."):
            value = separator.join((raw[:3], raw[3:5], raw[5:]))
            cases.append(
                (f"ssn-{seed}-{separator.strip() or 'space'}", f"SSN {value} filed")
            )
        cases.append((f"ssn-bare-{seed}", f"SSN {raw} filed"))
    cases += [
        ("ssn-mismatched", "SSN 219-09 9999 filed"),
        ("ssn-zero-area", "SSN 000-12-3456 is invalid"),
        ("ssn-666-area", "SSN 666-12-3456 is invalid"),
        ("ssn-zero-group", "SSN 219-00-9999 is invalid"),
        ("ssn-zero-serial", "SSN 219-09-0000 is invalid"),
    ]
    for seed in (100000001, 234567890, 111111111, 300000000):
        value = tckn(seed)
        cases.append((f"tckn-{seed}", f"TCKN {value} kayitli"))
        cases.append(
            (
                f"tckn-broken-{seed}",
                f"TCKN {value[:-1]}{(int(value[-1]) + 1) % 10} kayitli",
            )
        )
    for seed in (123456789, 100000000, 222222222):
        value = vkn(seed)
        cases.append((f"vkn-{seed}", f"VKN {value} kayitli"))
        cases.append((f"vkn-tax-{seed}", f"tax id {value} on file"))
        cases.append((f"vkn-vergi-{seed}", f"vergi kimlik {value} kayitli"))
        cases.append(
            (f"vkn-nocontext-{seed}", f"Reference number {value} appears here")
        )
        far = "x" * 40
        cases.append((f"vkn-farcontext-{seed}", f"vergi {far} {value} kayitli"))
    cases.append(("vkn-repeated-digits", "VKN 1111111111 kayitli"))
    return cases


def _secret_cases() -> list[tuple[str, str]]:
    cases = []
    for index, value in enumerate(_SECRET_VALUES):
        cases.append((f"secret-token-{index}", f"Use {value} for access"))
        cases.append((f"secret-token-trailing-{index}", f"Use {value}."))
    for index, key in enumerate(_SECRET_KEYS):
        cases.append((f"secret-assign-{index}", f"{key} = SuperSecret123!"))
        cases.append((f"secret-colon-{index}", f'{key}: "SuperSecret123!"'))
        cases.append((f"secret-quoted-{index}", f'"{key}" = "SuperSecret123!"'))
        cases.append((f"secret-json-{index}", f'{{"{key}": "SuperSecret123!"}}'))
        cases.append((f"secret-short-{index}", f"{key} = abc"))
    cases += [
        ("secret-flag-bare", "mysql --password=SuperSecret123!"),
        ("secret-flag-space", "mysql --password SuperSecret123!"),
        ("secret-flag-quoted", 'mysql --password="SuperSecret123!"'),
        (
            "secret-pem-complete",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----",
        ),
        (
            "secret-pem-unterminated",
            "-----BEGIN PRIVATE KEY-----\nMIIBOgIBAAJBAK\nrest of the file",
        ),
        (
            "secret-pem-openssh",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
        ),
        ("secret-env-block", "DB_PASS=hunter2222\nAPI_KEY=abcdef123456\n"),
    ]
    cases += [
        (f"secret-negative-{index}", text)
        for index, text in enumerate(_PROSE_NEGATIVES)
    ]
    return cases


def _uri_cases() -> list[tuple[str, str]]:
    cases = []
    for scheme in _DB_SCHEMES:
        value = f"{scheme}://alice:s3cr3tpw@db.example.com:5432/app"
        cases.append((f"uri-{scheme}", f"Connect {value} now"))
        cases.append((f"uri-trailing-{scheme}", f"Connect {value}."))
        cases.append((f"uri-quoted-{scheme}", f'Connect "{value}" now'))
        cases.append((f"uri-paren-{scheme}", f"Connect ({value})"))
    cases += [
        (
            "uri-embedded-secret",
            "postgresql://alice:sk_live_abcdefghijklmnop@db.example.com/app.",
        ),
        ("uri-upper-scheme", "Connect POSTGRESQL://alice:pw@db.example.com/app now"),
        ("uri-no-credentials", "Connect mysql://db.example.com/app now"),
        ("uri-http-negative", "Connect https://db.example.com/app now"),
    ]
    return cases


def _normalization_cases() -> list[tuple[str, str]]:
    return [
        ("norm-percent-at", "Contact alice%40example.com"),
        ("norm-percent-full", "Contact %61%6c%69%63%65%40example.com"),
        ("norm-percent-utf8", "Contact ali%C3%A7e@example.com"),
        ("norm-zwsp", "Contact alice​@example.com"),
        ("norm-soft-hyphen", "Contact alice­@example.com"),
        ("norm-bom", "Contact alice﻿@example.com"),
        ("norm-rlo", "Contact ‮alice@example.com‬"),
        ("norm-combining", "Contact alíce@example.com"),
        ("norm-fullwidth-card", "Card \uff14" + "\uff11" * 15),
        ("norm-fullwidth-at", "Contact alice\uff20example.com"),
        ("norm-hangul", "가 alice@example.com 한"),
        ("norm-hangul-jamo", "각 alice@example.com"),
        ("norm-nbsp", "Phone\xa0+90 532 123 45 67"),
        ("norm-ligature", "ﬁle alice@example.com"),
        ("norm-mixed", "Contact ali%63e​@example.com now"),
        ("norm-arabic", "البريد alice@example.com"),
        ("norm-emoji", "Contact \U0001f600 alice@example.com"),
    ]


def _overlap_cases() -> list[tuple[str, str]]:
    return [
        (
            "overlap-uri-secret",
            "postgresql://alice:sk_live_abcdefghijklmnop@db.example.com/app.",
        ),
        ("overlap-ssn-phone", "SSN 219-09-9999 and phone +1 415 555 2671"),
        ("overlap-card-in-uri", "postgresql://u:4111111111111111@db.example.com/app"),
        ("overlap-email-in-secret", "password = alice@example.com"),
        (
            "overlap-many",
            "alice@example.com 192.168.1.1 00:1A:2B:3C:4D:5E TR330006100519786457841326",
        ),
        ("overlap-adjacent", "alice@example.combob@example.org"),
    ]


def _edge_cases() -> list[tuple[str, str]]:
    return [
        ("edge-empty", ""),
        ("edge-space", "   "),
        ("edge-newlines", "\n\n\n"),
        ("edge-punctuation", '"' * 200 + " alice@example.com"),
        (
            "edge-repeat-emails",
            " ".join(f"user{index}@example.com" for index in range(40)),
        ),
        ("edge-long-word", "a" * 5_000 + " alice@example.com"),
        ("edge-tabs", "Contact\talice@example.com\tnow"),
        ("edge-crlf", "Contact alice@example.com\r\npassword = SuperSecret123!"),
    ]


def cases() -> list[tuple[str, str]]:
    collected: list[tuple[str, str]] = []
    for family in (
        _email_cases,
        _phone_cases,
        _card_cases,
        _iban_cases,
        _network_cases,
        _identity_cases,
        _secret_cases,
        _uri_cases,
        _normalization_cases,
        _overlap_cases,
        _edge_cases,
    ):
        collected.extend(family())
    identifiers = [identifier for identifier, _ in collected]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("parity case identifiers must be unique")
    return collected


def build() -> dict[str, object]:
    from shim_cli.guard import evaluate

    records = []
    for identifier, text in cases():
        decision = evaluate(text)
        records.append(
            {
                "id": identifier,
                "text": text,
                "findings": [
                    [finding.entity_type, finding.start, finding.end, finding.score]
                    for finding in decision.findings
                ],
                "redacted": decision.redacted_text,
            }
        )
    return {"version": CORPUS_VERSION, "case_count": len(records), "cases": records}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing oracle. This re-baselines the safety net "
        "against whatever the detector does today; it does not verify it.",
    )
    args = parser.parse_args()
    if args.output.exists() and not args.force:
        raise SystemExit(
            f"{args.output} already exists. Re-running would replace the frozen "
            "expectations with current behaviour instead of checking against "
            "them. Pass --force only when deliberately re-baselining."
        )
    document = build()
    args.output.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    findings = sum(len(case["findings"]) for case in document["cases"])
    print(f"{document['case_count']} cases, {findings} findings -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
