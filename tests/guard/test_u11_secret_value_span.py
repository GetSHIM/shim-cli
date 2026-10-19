"""A secret's placeholder covers its value: not the option, parameter, bracket
or attribute name beside it, and never a neighbour while the value stays in
clear text."""

from __future__ import annotations

import gc
import json
import random
import string
import time
from urllib.parse import quote

import pytest

from shim_cli.guard import ENTITY_TYPES, evaluate

# Generated passwords hold every printable character but the ones that end a
# bare value as before this change (whitespace, `,`, `}`, `]` and quotes) and
# `%`, which starts a percent escape the normaliser decodes before detection.
_PASSWORD_CHARACTERS = "".join(
    c for c in string.printable if c not in string.whitespace + ",}]\"'%"
)


def _passwords(characters: str, seed: int) -> list[str]:
    generator = random.Random(seed)
    return [
        "".join(generator.choice(characters) for _ in range(16)) for _ in range(1_000)
    ]


def _a_variable(password: str) -> bool:
    """`$NAME` reads as a shell variable, under any key."""
    return password.startswith("$") and password[1:].isidentifier()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Server=sql01;Database=Orders;User Id=app;Password=Hunter2abc9;"
            "TrustServerCertificate=True;Encrypt=False;",
            "Server=sql01;Database=Orders;User Id=app;Password=<SECRET_1>;"
            "TrustServerCertificate=True;Encrypt=False;",
        ),
        (
            "https://api.example.com/v1/items?api_key=abc123def456&page=2&format=json",
            "https://api.example.com/v1/items?api_key=<SECRET_1>&page=2&format=json",
        ),
        ("Server=db;Password=s3cr3tPass;", "Server=db;Password=<SECRET_1>;"),
        (
            "Server=db;Password=s3cr3tPass;Integrated Security=false",
            "Server=db;Password=<SECRET_1>;Integrated Security=false",
        ),
        (
            "Server=db; Password=s3cr3tPass; Database=app",
            "Server=db; Password=<SECRET_1>; Database=app",
        ),
        ("https://h/x?token=abc123def456&", "https://h/x?token=<SECRET_1>&"),
        ("`DB_PASSWORD=s3cr3tPass1`", "`DB_PASSWORD=<SECRET_1>`"),
        ("f(g(token=abc123def456));", "f(g(token=<SECRET_1>));"),
        ("f(password=get_password())", "f(password=<SECRET_1>)"),
        ("f(password=Xk9(mP2qL7))", "f(password=<SECRET_1>)"),
        ("$(login --password s3cr3tPass1)", "$(login --password <SECRET_1>)"),
        ("(token abc123def;)", "(token <SECRET_1>)"),
        # The `)` closes the inner `(`; the outer one stays open.
        ("(x (password=abc123)", "(x (password=<SECRET_1>)"),
    ],
)
def test_a_delimiter_after_the_value_stays_outside_the_placeholder(
    text: str, expected: str
) -> None:
    decision = evaluate(text)

    assert decision.counts == (("SECRET", 1),)
    assert decision.redacted_text == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Outside a list of fields a `;`, `&` or `|` may be the password's own.
        ("DB_PASSWORD=Xk9&mP2qL7", "DB_PASSWORD=<SECRET_1>"),
        ("DB_PASSWORD=Xk9;a=mP2qL7zz", "DB_PASSWORD=<SECRET_1>"),
        ("DB_PASSWORD=Xk9&b=mP2qL7xyz", "DB_PASSWORD=<SECRET_1>"),
        ("DB_PASSWORD=&k9E=mP2qL7", "DB_PASSWORD=<SECRET_1>"),
        ("DB_PASSWORD=s3cr3t|Pass", "DB_PASSWORD=<SECRET_1>"),
        ("DB_PASSWORD=Xk9mP2qL7&", "DB_PASSWORD=<SECRET_1>"),
        ("DB_PASSWORD=Xk9mP2qL7;", "DB_PASSWORD=<SECRET_1>"),
        ("DB_PASSWORD=;;&&))", "DB_PASSWORD=<SECRET_1>"),
        ("PASSWORD=s3cr3tPass|next", "PASSWORD=<SECRET_1>"),
        ("Password=abc;Encrypt=true", "Password=<SECRET_1>"),
        ('"Password=Hunter2abc9;"', '"Password=<SECRET_1>"'),
        # A shell line's `;` ends the command, but nothing tells it from a
        # .env value's last character.
        ("export DB_PASSWORD=s3cr3tPass1;", "export DB_PASSWORD=<SECRET_1>"),
        ("password abc123def;", "password <SECRET_1>"),
        # A closing mark the line did not open is the password's.
        ("DB_PASSWORD=Xk9mP2qL7)", "DB_PASSWORD=<SECRET_1>"),
        ("DB_PASSWORD=pass(word)x", "DB_PASSWORD=<SECRET_1>"),
        ("DB_PASSWORD=Xk9(mP2qL7)", "DB_PASSWORD=<SECRET_1>"),
        ("DB_PASSWORD=Ab<9xQ!z>", "DB_PASSWORD=<SECRET_1>"),
        ("password = get_password()", "password = <SECRET_1>"),
        # In a list, only the list's own separator ends a value.
        (
            "Server=db;Password=Xk9&a=mP2;Database=app",
            "Server=db;Password=<SECRET_1>;Database=app",
        ),
        # A value the cut would leave unbalanced keeps the mark it may close.
        ("f(password=Xk9mP2qL7))", "f(password=<SECRET_1>"),
        ("f(password=Xk9)mP2qL7)", "f(password=<SECRET_1>"),
        ("`DB_PASSWORD=Xk9`mP2qL7`", "`DB_PASSWORD=<SECRET_1>"),
    ],
)
def test_a_delimiter_inside_a_password_is_masked_with_it(
    text: str, expected: str
) -> None:
    assert evaluate(text).redacted_text == expected


@pytest.mark.parametrize(
    "text",
    [
        "Server=localhost;Uid=root;Pwd=;Database=app;Port=3306",
        "Server=localhost;User Id=sa;Password=;Encrypt=true;TrustServerCertificate=True",
        "https://x/cb?token=&page=2&format=json",
        "user=admin|password=|role=ops",
    ],
)
def test_an_empty_listed_value_leaves_the_next_field_alone(text: str) -> None:
    decision = evaluate(text)

    assert decision.counts == ()
    assert decision.redacted_text == text


@pytest.mark.parametrize(
    ("line", "outside"),
    [
        ("PASSWORD={}", ""),
        ("Server=db;Password={};Database=app", ";"),
        ("Server=db;Password={}", ";"),
        ("https://h/x?token={}&page=2", "&"),
        ("fetch(`https://x?token={}&limit=50`)", "&"),
        ("user=admin|password={}|role=ops", "|"),
    ],
)
@pytest.mark.parametrize("seed", [33, 7, 101])
def test_a_generated_password_is_masked_whole(
    line: str, outside: str, seed: int
) -> None:
    """Any character, the last one too, `=` and every delimiter included. A
    list's own separator is left out of passwords in that list, because its
    grammar makes the separator end the value there."""
    characters = "".join(c for c in _PASSWORD_CHARACTERS if c not in outside)

    for password in _passwords(characters, seed):
        expected = password if _a_variable(password) else "<SECRET_1>"

        assert evaluate(line.format(password)).redacted_text == line.format(expected), (
            password
        )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "https://h/x?password=Xk9%26a%3DmP2qL7zz&page=2",
            "https://h/x?password=<SECRET_1>&page=2",
        ),
        (
            'curl "https://api.example.com/login?user=bob&password=Pa55%26w%3Drd99'
            '&next=/home"',
            'curl "https://api.example.com/login?user=bob&password=<SECRET_1>'
            '&next=/home"',
        ),
        (
            "Server=db;Password=Ab1%3Bx%3DZq9secret;Database=app",
            "Server=db;Password=<SECRET_1>;Database=app",
        ),
        (
            "level=info|password=Ab1%7Cx%3DZq9secret|ip=x",
            "level=info|password=<SECRET_1>|ip=x",
        ),
        (
            "https://h/x?token=abc123%2Cdef&page=2",
            "https://h/x?token=<SECRET_1>&page=2",
        ),
        ("(token=abc123%29def)", "(token=<SECRET_1>)"),
        ("(token=abc123def%29", "(token=<SECRET_1>"),
        (
            "Server=db;Password=Xk9mP2qL7\uff1bx=1;Database=app",
            "Server=db;Password=<SECRET_1>;Database=app",
        ),
        # A quote a percent escape writes is the password's too: a value
        # quoted with a quote its source writes ends at the next such quote.
        ('PASSWORD="Xk9mP2q%22L7zz"', 'PASSWORD="<SECRET_1>"'),
        ('?password="Xk9mP2q%22L7zz"&page=2', '?password="<SECRET_1>"&page=2'),
        ("api_key='Xk9mP2q%27L7zz'", "api_key='<SECRET_1>'"),
        ('password "Xk9mP2q%22L7zz"', 'password "<SECRET_1>"'),
        (
            '<input name="password" value="Xk9mP2qL7zz%22tail">',
            '<input name="password" value="<SECRET_1>">',
        ),
        (
            "<add key='Password' value='s3cr3tP%27ass1'/>",
            "<add key='Password' value='<SECRET_1>'/>",
        ),
        (
            '{"config": "<add key=\\"Password\\" value=\\"s3cr3tPass1%22x\\"/>"}',
            '{"config": "<add key=\\"Password\\" value=\\"<SECRET_1>\\"/>"}',
        ),
        # The bare `--password` reading takes the opening quote, as before.
        ("--password='Xk9mP2q%27L7 zz' -h db", "--password=<SECRET_1>' -h db"),
        (
            '{"identitytoken": "Xk9mP2qL%22L7zz"}',
            '{"identitytoken": "<SECRET_1>"}',
        ),
        # With no closing quote its source writes, it runs to its line's end.
        ('PASSWORD="Xk9mP2q%22L7zz\nUSER=bob', 'PASSWORD="<SECRET_1>\nUSER=bob'),
    ],
)
def test_a_decoded_character_never_ends_a_value(text: str, expected: str) -> None:
    """Only a separator or bracket written as itself ends a value: one a
    percent escape or a compatibility form decodes to is the password's."""
    decision = evaluate(text)

    assert decision.counts == (("SECRET", 1),)
    assert decision.redacted_text == expected


@pytest.mark.parametrize(
    "line",
    [
        "https://h/x?token={}&page=2",
        "https://h/x?a=1&password={}",
        "Server=db;Password={};Database=app",
        "user=admin|password={}|role=ops",
        "f(password={})",
    ],
)
@pytest.mark.parametrize("seed", [33, 7, 101])
def test_a_generated_percent_encoded_password_is_masked_whole(
    line: str, seed: int
) -> None:
    """Every character but those that end a bare value's first six, the
    list's own separator, `%` and `=` included, written as a URL writes it."""
    characters = string.digits + string.ascii_letters + string.punctuation
    characters = "".join(c for c in characters if c not in ",}]\"'")

    for password in _passwords(characters, seed):
        encoded = quote(password, safe="")
        expected = encoded if _a_variable(password) else "<SECRET_1>"

        assert evaluate(line.format(encoded)).redacted_text == line.format(expected), (
            password
        )


@pytest.mark.parametrize(
    "line", ["f(password={})", "$(login --password {})", "`PASSWORD={}`"]
)
@pytest.mark.parametrize("seed", [33, 7, 101])
def test_a_generated_password_on_a_balanced_line_never_loses_a_character(
    line: str, seed: int
) -> None:
    """A password with a closing mark of its own may take the line's closing
    mark into its placeholder, but never leaves one of its characters out, on
    a line whose marks balance."""
    prefix, suffix = line.split("{}")
    masked = prefix + "<SECRET_1>"

    for password in _passwords(_PASSWORD_CHARACTERS, seed):
        if _a_variable(password):
            continue
        decision = evaluate(line.format(password))

        assert decision.counts == (("SECRET", 1),), password
        assert decision.redacted_text.startswith(masked), password
        assert suffix.endswith(decision.redacted_text[len(masked) :]), password


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '<add key="Password" value="S3cretPass1" />',
            '<add key="Password" value="<SECRET_1>" />',
        ),
        (
            '<input type="text" name="password" value="S3cretPass1">',
            '<input type="text" name="password" value="<SECRET_1>">',
        ),
        (
            "<add key='ApiKey' value='0123456789abcdef'/>",
            "<add key='ApiKey' value='<SECRET_1>'/>",
        ),
        (
            '<property name="spring.datasource.password" value="s3cr3tPass1"/>',
            '<property name="spring.datasource.password" value="<SECRET_1>"/>',
        ),
        (
            '<add key="Smtp:Password"\n     value="s3cr3tPass1" />',
            '<add key="Smtp:Password"\n     value="<SECRET_1>" />',
        ),
        (
            '<input name=password value="s3cr3tPass1">',
            '<input name=password value="<SECRET_1>">',
        ),
        (
            '{"config": "<add key=\\"Password\\" value=\\"s3cr3tPass1\\"/>"}',
            '{"config": "<add key=\\"Password\\" value=\\"<SECRET_1>\\"/>"}',
        ),
        # A key ending in a legacy key keeps a numeric password, as
        # `jdbc.password=12345678` does.
        (
            '<entry key="jdbc.password" value="12345678"/>',
            '<entry key="jdbc.password" value="<SECRET_1>"/>',
        ),
        (
            '<add key="Smtp:Password" value="20231234"/>',
            '<add key="Smtp:Password" value="<SECRET_1>"/>',
        ),
        (
            '<add key="ConnectionStrings:Password" value="20231234"/>',
            '<add key="ConnectionStrings:Password" value="<SECRET_1>"/>',
        ),
        (
            '<property name="spring.datasource.password" value="12345678"/>',
            '<property name="spring.datasource.password" value="<SECRET_1>"/>',
        ),
        # Bare and unquoted attributes in between, the value first, the field's
        # type naming it, a meta tag's content.
        (
            '<input name="password" disabled value="S3cretPass1">',
            '<input name="password" disabled value="<SECRET_1>">',
        ),
        (
            '<input name="password" required autofocus size=20 value="S3cretPass1">',
            '<input name="password" required autofocus size=20 value="<SECRET_1>">',
        ),
        (
            '<input value="S3cretPass1" name="password">',
            '<input value="<SECRET_1>" name="password">',
        ),
        (
            '<input type="password" value="S3cretPass1">',
            '<input type="password" value="<SECRET_1>">',
        ),
        (
            '<meta name="csrf-token" content="Xk9mP2qL7abc">',
            '<meta name="csrf-token" content="<SECRET_1>">',
        ),
        # A value starting with a space or a comma is the value, and
        # `value=` stays.
        (
            '<input name="password" value=" s3cr3tPass1">',
            '<input name="password" value="<SECRET_1>">',
        ),
        (
            '<input name="password" value=",s3cr3tPass1">',
            '<input name="password" value="<SECRET_1>">',
        ),
        # Unquoted, after a space or a line break, named by any attribute.
        (
            '<input name="password" value= s3cr3tPass1>',
            '<input name="password" value= <SECRET_1>>',
        ),
        (
            "<input name=password value= s3cr3tPass1>",
            "<input name=password value= <SECRET_1>>",
        ),
        (
            '<input id="password" value= "s3cr3tPass1">',
            '<input id="password" value= "<SECRET_1>">',
        ),
        (
            "<input type=password value=s3cr3tPass1>",
            "<input type=password value=<SECRET_1>>",
        ),
        (
            "<input name=password value=Xk9mP2qL7z1>",
            "<input name=password value=<SECRET_1>>",
        ),
        (
            "<input value=Xk9mP2qL7z1 name=password>",
            "<input value=<SECRET_1> name=password>",
        ),
        (
            '<input name="password" value=\n"s3cr3tPass1">',
            '<input name="password" value=\n"<SECRET_1>">',
        ),
        (
            '<input id="password" value="s3cr3tPass1">',
            '<input id="password" value="<SECRET_1>">',
        ),
        (
            '<input id=password value="s3cr3tPass1">',
            '<input id=password value="<SECRET_1>">',
        ),
        (
            '<input id="password" class="x" value="s3cr3tPass1">',
            '<input id="password" class="x" value="<SECRET_1>">',
        ),
        (
            '<x data-name="password" value="Xk9mP2qL7z1">',
            '<x data-name="password" value="<SECRET_1>">',
        ),
        (
            '<x data-name="password" class="x" value="Xk9mP2qL7z1">',
            '<x data-name="password" class="x" value="<SECRET_1>">',
        ),
        (
            '<x name="password" data-value="Xk9mP2qL7z1">',
            '<x name="password" data-value="<SECRET_1>">',
        ),
        ('"token" value="Xk9mP2qL7z1"', '"token" value="<SECRET_1>"'),
        ("password value=s3cr3tPass1", "password value=<SECRET_1>"),
        # An unquoted value runs to whitespace or `>`, as a browser reads it:
        # `=`, `<`, a backtick or a quote is the password's, and so is a
        # character a percent escape writes, a space or `>` among them.
        (
            "<input name=password value=Xk9=mP2qL7zz>",
            "<input name=password value=<SECRET_1>>",
        ),
        (
            '<input name="password" value=ab=cdefgh123>',
            '<input name="password" value=<SECRET_1>>',
        ),
        ("password value=Xk9=mP2qL7zz", "password value=<SECRET_1>"),
        (
            '<input name="password" value=Xk9mP2qL=7zzQ>',
            '<input name="password" value=<SECRET_1>>',
        ),
        (
            "<input type=password value=s3cr3tPass1=xyzQ>",
            "<input type=password value=<SECRET_1>>",
        ),
        (
            "<input name=password value=Xk9mP2qL`7zzQ>",
            "<input name=password value=<SECRET_1>>",
        ),
        (
            "<input name=password value=Xk9<mP2qL7>",
            "<input name=password value=<SECRET_1>>",
        ),
        (
            "<input name=password value=Xk9mP2'qL7>",
            "<input name=password value=<SECRET_1>>",
        ),
        (
            "<input name=password value=Xk9%3DmP2qL7>",
            "<input name=password value=<SECRET_1>>",
        ),
        (
            '<input type="password" value=Xk9mP2qL%3D7zzQ>',
            '<input type="password" value=<SECRET_1>>',
        ),
        (
            "<input name=password value=Xk9%3EmP2qL7>",
            "<input name=password value=<SECRET_1>>",
        ),
        (
            "<input name=password value=Xk9%20mP2qL7>",
            "<input name=password value=<SECRET_1>>",
        ),
        (
            "<input value=Xk9=mP2qL7 name=password>",
            "<input value=<SECRET_1> name=password>",
        ),
        (
            "<input value=Xk9%20mP2qL7 name=password>",
            "<input value=<SECRET_1> name=password>",
        ),
        # An attribute between the two may hold `=` unquoted too.
        (
            '<input name="password" nonce=abc= value=s3cr3tPass1>',
            '<input name="password" nonce=abc= value=<SECRET_1>>',
        ),
    ],
)
def test_a_secret_attribute_pair_masks_the_value_attribute(
    text: str, expected: str
) -> None:
    decision = evaluate(text)

    assert decision.counts == (("SECRET", 1),)
    assert decision.redacted_text == expected


@pytest.mark.parametrize(
    "text",
    [
        '<input name="password" value="">',
        '<input type="password" name="password" placeholder="Password">',
        '<add key="Theme" value="dark-mode-1"/>',
        '<add key="PasswordMinLength" value="12"/>',
        '<add key="TokenUrl" value="https://auth.example.com/token"/>',
        '<add key="Password" value="${DB_PASSWORD}"/>',
        '<add key="Password" value="abc"/>',
        '<input name="password"> <input value="dark-mode-1">',
        '<meta name="description" content="Rotate the token daily">',
        # The attribute after a secret's name is not its value, and an empty
        # value attribute holds nothing.
        '<input name="password" value=>',
        "password value=",
        '<input name="password" id=pwfield1>',
        '<input name="password" autocomplete=current-password>',
        '<label for="password" class=form-label>',
        '<input type="password" name=pw1234 id=pass_word>',
        # A bound attribute holds an expression.
        '<input :value="form.password" name="password">',
        '<input name="password" th:value="${user.password}">',
    ],
)
def test_an_attribute_pair_with_no_secret_value_is_left_alone(text: str) -> None:
    decision = evaluate(text)

    assert decision.counts == ()
    assert decision.redacted_text == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '<meta content="token QWxhZGRpbjpvcGVuc2VzYW1l=">',
            '<meta content="token <SECRET_1>">',
        ),
        (
            'const h = "token QWxhZGRpbjpvcGVuc2VzYW1l=".trim();',
            'const h = "token <SECRET_1>".trim();',
        ),
        (
            "x = 'secret QWxhZGRpbjpvcGVuc2VzYW1l='+y",
            "x = 'secret <SECRET_1>'+y",
        ),
        (
            'x = "token QWxhZGRpbjpvcGVuc2VzYW1l=".strip()',
            'x = "token <SECRET_1>".strip()',
        ),
        # After a quoted key, a value with one `=` of padding is no attribute.
        ('"token" aGVsbG8xMjM=', '"token" <SECRET_1>'),
        ("name=token aGVsbG8xMjM=", "name=token <SECRET_1>"),
    ],
)
def test_a_padded_value_after_a_spaced_key_is_no_attribute(
    text: str, expected: str
) -> None:
    decision = evaluate(text)

    assert decision.counts == (("SECRET", 1),)
    assert decision.redacted_text == expected


def test_aws_secrets_manager_output_masks_the_secret_string() -> None:
    text = (
        '{\n    "Name": "prod/db",\n    "SecretString": '
        '"{\\"username\\":\\"admin\\",\\"password\\":\\"S3cretPass1\\"}",\n'
        '    "VersionStages": ["AWSCURRENT"]\n}'
    )

    decision = evaluate(text)

    assert decision.counts == (("SECRET", 1),)
    assert decision.redacted_text == (
        '{\n    "Name": "prod/db",\n    "SecretString": "<SECRET_1>",\n'
        '    "VersionStages": ["AWSCURRENT"]\n}'
    )


def _logged(password: str) -> str:
    return json.dumps(
        {"event": "login", "body": json.dumps({"user": "admin", "password": password})}
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '{"event":"request","payload":"{\\"password\\": \\"s3cr3tPass1\\"}"}',
            '{"event":"request","payload":"{\\"password\\": \\"<SECRET_1>\\"}"}',
        ),
        (
            'msg="{\\"api_key\\":\\"0123456789abcdef\\",\\"page\\":2}"',
            'msg="{\\"api_key\\":\\"<SECRET_1>\\",\\"page\\":2}"',
        ),
        ('password="abcdef\\"', 'password="<SECRET_1>"'),
        # A closing quote is followed by what is around the value, not by a
        # word: here the backslash ends the value.
        ('password="abcdef\\" other="x"', 'password="<SECRET_1>" other="x"'),
        ('password="ab\\"cd"', 'password="<SECRET_1>"'),
        ("DB_PASSWORD='Xa\\[aY'", "DB_PASSWORD='<SECRET_1>'"),
        ('"password": "abcdef\\\\"', '"password": "<SECRET_1>"'),
        ('password="abcd\\\\\\"efg"', 'password="<SECRET_1>"'),
        # A quote or a backslash inside a value an escaped quote opened, at
        # any place in it.
        (_logged('Xk9mP2qL7"tail9Zq'), _logged("<SECRET_1>")),
        (_logged('abc"defghij'), _logged("<SECRET_1>")),
        (_logged('abcd"efghij'), _logged("<SECRET_1>")),
        (_logged("ab\\cdefghij"), _logged("<SECRET_1>")),
        (_logged("abcdefgh\\"), _logged("<SECRET_1>")),
        (
            "{'payload': '{\\'password\\': \\'abcdefg\\\\\\'hij1XYZ\\'}'}",
            "{'payload': '{\\'password\\': \\'<SECRET_1>\\'}'}",
        ),
    ],
)
def test_an_escaped_quote_bounds_the_value_it_opened(text: str, expected: str) -> None:
    decision = evaluate(text)

    assert decision.counts == (("SECRET", 1),)
    assert decision.redacted_text == expected


@pytest.mark.parametrize("seed", [33, 7, 101])
def test_a_generated_password_in_json_or_json_inside_json_is_masked_whole(
    seed: int,
) -> None:
    characters = "".join(c for c in string.printable if c not in "\t\n\r\x0b\x0c%")

    for wrap in (lambda p: json.dumps({"user": "admin", "password": p}), _logged):
        for password in _passwords(characters, seed):
            expected = password if _a_variable(password) else "<SECRET_1>"

            assert evaluate(wrap(password)).redacted_text == wrap(expected), password


@pytest.mark.parametrize(
    "text",
    [
        'String s = "token=\\"" + token + "\\"";',
        'sb.append("password=\\"").append(pw).append("\\"");',
        'json = "{\\"password\\": \\"" + password + "\\"}"',
        'curl -d "{\\"password\\":\\"$PASS\\",\\"user\\":\\"$USER\\"}"',
    ],
)
def test_code_that_builds_an_escaped_value_is_no_secret(text: str) -> None:
    decision = evaluate(text)

    assert decision.counts == ()
    assert decision.redacted_text == text


def _cpu(text: str) -> tuple[float, float]:
    """Wall and CPU seconds of one evaluation, from a collected heap: a full
    collection the earlier tests' objects bring due mid-run scans all of them,
    and cost the larger run 0.16 s on its own."""
    gc.collect()
    wall, cpu = time.perf_counter(), time.process_time()
    evaluate(text, ENTITY_TYPES)
    return time.perf_counter() - wall, time.process_time() - cpu


@pytest.mark.parametrize(
    ("shape", "count"),
    [
        (lambda count: "token=" * count + ")" * count, 8_000),
        (lambda count: ("token=" * 150 + "x(" + ")" * 50 + " ") * count, 40),
        (lambda count: "(token=abcdef" + ")" * count, 40_000),
        (lambda count: ("(" * 500 + "token=abcdef" + ")" * 500 + " ") * count, 40),
        (lambda count: "token=abcdef" + ";a" * count, 40_000),
        (lambda count: "password=a;" * count, 10_000),
        (lambda count: ";password=a" * count, 10_000),
        (lambda count: ";password=" + ";" * count, 40_000),
        (lambda count: 'name="password" ' * count, 10_000),
        (lambda count: '<add key="Password" a="b"' * count, 5_000),
        (lambda count: '<a value="x"' + " b" * count, 20_000),
        (lambda count: '\\"password\\":\\"' * count, 10_000),
        (lambda count: "password='" + '\\"' * count, 40_000),
        (lambda count: 'password="' + "\\" * count, 80_000),
        (lambda count: 'password=\\"' + "\\\\" * count, 40_000),
        (lambda count: "value=" * count, 10_000),
        (lambda count: 'value="' * count, 10_000),
        (lambda count: 'password="abcdefg%22' * count, 10_000),
    ],
    ids=[
        "nested-values-closing-marks",
        "nested-values-opened-marks",
        "closing-marks",
        "brackets",
        "separators",
        "separated-keys",
        "listed-keys",
        "listed-separators",
        "attribute-keys",
        "attribute-pairs",
        "bare-attributes",
        "escaped-keys",
        "escaped-quotes",
        "backslashes",
        "escaped-backslashes",
        "unquoted-values",
        "unclosed-values",
        "decoded-closing-quotes",
    ],
)
def test_the_value_bounds_are_read_in_linear_time(shape, count: int) -> None:
    half = _cpu(shape(count // 2))[1]
    wall, full = _cpu(shape(count))

    assert wall < 2
    assert full <= 2.5 * half + 0.05, (half, full)
