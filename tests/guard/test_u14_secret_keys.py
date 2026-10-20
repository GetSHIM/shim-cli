"""A secret's key is read as people write it: glued to a prefix in one case,
before a type annotation, as an element's name, or in a name field paired with
a value field. Under a key that ends in a password word, every value is a
secret unless it only refers to one or reads as code made of names."""

from __future__ import annotations

import random
import string
import time

import pytest

from shim_cli.guard import evaluate


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("PGPASSWORD=example-password-0000", "PGPASSWORD=<SECRET_1>"),
        ("export PGPASSWORD=example-password-0000", "export PGPASSWORD=<SECRET_1>"),
        ("    PGPASSWORD: example-password-0000", "    PGPASSWORD: <SECRET_1>"),
        ("DBPASSWORD=example-password-0000", "DBPASSWORD=<SECRET_1>"),
        ("ADMINPASSWORD=example-password-0000", "ADMINPASSWORD=<SECRET_1>"),
        ("SECRETKEY=example-password-0000", "SECRETKEY=<SECRET_1>"),
        ("APPSECRETKEY=example-password-0000", "APPSECRETKEY=<SECRET_1>"),
        ("ACCESSTOKEN=example-password-0000", "ACCESSTOKEN=<SECRET_1>"),
        ("CLIENTSECRET=example-password-0000", "CLIENTSECRET=<SECRET_1>"),
        ("OPENAIAPIKEY=example-password-0000", "OPENAIAPIKEY=<SECRET_1>"),
        ("pgpassword=example-password-0000", "pgpassword=<SECRET_1>"),
        ('{"clientsecret": "example-password-0000"}', '{"clientsecret": "<SECRET_1>"}'),
        ("DBPassword=example-password-0000", "DBPassword=<SECRET_1>"),
        ("PGPASSWORD=12345678", "PGPASSWORD=<SECRET_1>"),
        ("refreshtoken: example-password-0000", "refreshtoken: <SECRET_1>"),
        ('{"bearertoken": "example-password-0000"}', '{"bearertoken": "<SECRET_1>"}'),
        ("githubtoken=example-password-0000", "githubtoken=<SECRET_1>"),
        ("openaiapikey=example-password-0000", "openaiapikey=<SECRET_1>"),
        (
            '{"auths": {"registry.example.com": '
            '{"registrytoken": "example-password-0000"}}}',
            '{"auths": {"registry.example.com": {"registrytoken": "<SECRET_1>"}}}',
        ),
        ("PGPASSWORD_PROD=example-password-0000", "PGPASSWORD_PROD=<SECRET_1>"),
        ("DBPASSWORD.prod=example-password-0000", "DBPASSWORD.prod=<SECRET_1>"),
        ("PGPASSWORD2=example-password-0000", "PGPASSWORD2=<SECRET_1>"),
        ("DB_PASSWORD2=example-password-0000", "DB_PASSWORD2=<SECRET_1>"),
        ("password1: example-password-0000", "password1: <SECRET_1>"),
        ("PGPASSWORD2=12345678", "PGPASSWORD2=<SECRET_1>"),
    ],
)
def test_a_key_with_its_secret_word_glued_on_is_a_key(text: str, expected: str) -> None:
    assert evaluate(text).redacted_text == expected


@pytest.mark.parametrize(
    "text",
    [
        "TOKENIZER=bert-base-uncased",
        "MAX_TOKENS=4096",
        "MAXTOKENS=4096",
        "TOKEN_TTL=3600",
        "TOKENTTL=3600",
        "SESSION_TOKEN_TTL=604800",
        "PASSWORD_MIN_LENGTH=12",
        "PASSWORDLESS=enabled-for-all",
        "BYPASS=0123456789abcdef",
        "COMPASS=north-north-west",
        "LOWPASS=2000000",
        "secretary=0123456789abcdef",
        "PGPASSWORD_FILE=/run/secrets/pgpass",
        "PGPASSWORD=$PGPASS_FROM_VAULT",
        "tokenizer=bert-base-uncased",
        "maxtokens=4096",
        "tokenttl=3600",
        "PGPASSWORD_MIN_LENGTH=12",
        '"identitytoken": "<your-identity-token>"',
        '"registrytoken": "<your-registry-token>"',
        '"api_token": "<your-api-token>"',
        "API_TOKEN=<YOUR_API_TOKEN>",
    ],
)
def test_a_word_that_only_contains_a_secret_word_stays_quiet(text: str) -> None:
    assert evaluate(text).counts == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '    db_password: str = "example-password-0000"',
            '    db_password: str = "<SECRET_1>"',
        ),
        (
            "    db_password: str = 'example-password-0000'",
            "    db_password: str = '<SECRET_1>'",
        ),
        (
            'api_key: Optional[str] = "example-password-0000"',
            'api_key: Optional[str] = "<SECRET_1>"',
        ),
        (
            'smtp_password: str | None = "example-password-0000"',
            'smtp_password: str | None = "<SECRET_1>"',
        ),
        (
            'const password: string = "example-password-0000";',
            'const password: string = "<SECRET_1>";',
        ),
        (
            'private readonly apiToken: string = "example-password-0000";',
            'private readonly apiToken: string = "<SECRET_1>";',
        ),
        (
            'val dbPassword: String = "example-password-0000"',
            'val dbPassword: String = "<SECRET_1>"',
        ),
        (
            'let clientSecret: String = "example-password-0000"',
            'let clientSecret: String = "<SECRET_1>"',
        ),
        (
            'const DB_PASSWORD: &str = "example-password-0000";',
            'const DB_PASSWORD: &str = "<SECRET_1>";',
        ),
        (
            'static API_TOKEN: &\'static str = "example-password-0000";',
            'static API_TOKEN: &\'static str = "<SECRET_1>";',
        ),
        (
            'password?: string = "example-password-0000";',
            'password?: string = "<SECRET_1>";',
        ),
        ("    db_password: int = 12345678", "    db_password: int = <SECRET_1>"),
    ],
)
def test_a_typed_declaration_masks_its_initializer(text: str, expected: str) -> None:
    assert evaluate(text).redacted_text == expected


@pytest.mark.parametrize(
    "text",
    [
        "api_key: Optional[str] = None",
        "password: str = None",
        'db_password: str = os.environ["DB_PASSWORD"]',
        "secret_backend: Mapped[str] = mapped_column()",
        "token: str = Field(default=None)",
        'password: str = ""',
        "if password: x == 'abcdefgh'",
    ],
)
def test_a_typed_declaration_without_a_literal_stays_quiet(text: str) -> None:
    assert evaluate(text).counts == ()


@pytest.mark.parametrize(
    "text",
    [
        "password: pa55word=abcdefg",
        'password: pa55word="abcdefg"',
        'db_password: Kx9w="Lm2pQr7z"',
        'db_password: Kxwm="Lm2pQr7z"',
        'password: s3cr3tPassw0rd = "something long"',
        'DB_PASSWORD: Hunter2Hunter2 = "abcdefgh"',
        'api_key: sk_live_51Habcdefgh0000 = "comment here"',
        "PASSWORD: Pa55w0rd, = 'abcdefgh'",
        "password: Pa55w0rd = 12345678",
        'password: Annotated[str, Field(min_length=8)] = "example-password-0000"',
    ],
)
def test_a_value_that_resembles_an_annotation_is_masked_whole(text: str) -> None:
    """Only words make a type. Text with a digit, or glued to its `=`, may be
    the password itself, so the mask runs through it to the initializer's end."""
    key = text.partition(":")[0]

    assert evaluate(text).redacted_text == f"{key}: <SECRET_1>"


_SETTINGS_XML = """<settings>
  <servers>
    <server>
      <id>nexus-releases</id>
      <username>deploy</username>
      <password>example-password-0000</password>
    </server>
  </servers>
</settings>"""

_K8S_ENV = """      env:
        - name: DB_PASSWORD
          value: "example-password-0000"
        - name: LOG_LEVEL
          value: verbose-mode"""

_K8S_SECRET_REF = """      env:
        - name: DB_PASSWORD
          valueFrom:
            secretKeyRef:
              name: db-credentials
              key: password
        - name: LOG_LEVEL
          value: verbose-mode"""

_ECS = '{"environment": [{"name": "API_TOKEN", "value": "example-password-0000"}]}'

_SSM = """{
    "Parameter": {
        "Name": "/prod/db/password",
        "Type": "SecureString",
        "Value": "example-password-0000",
        "Version": 3
    }
}"""

_TERRAFORM = """variable "db_password" {
  description = "Password for the primary database"
  type        = string
  sensitive   = true
  default     = "example-password-0000"
}"""


@pytest.mark.parametrize(
    "text",
    [_SETTINGS_XML, _K8S_ENV, _ECS, _SSM, _TERRAFORM],
    ids=["settings-xml", "k8s-env", "ecs", "ssm", "terraform"],
)
def test_a_value_named_by_its_element_or_name_field_is_masked(text: str) -> None:
    decision = evaluate(text)

    assert decision.counts == (("SECRET", 1),)
    assert decision.redacted_text == text.replace("example-password-0000", "<SECRET_1>")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "<keyPassword>example-password-0000</keyPassword>",
            "<keyPassword><SECRET_1></keyPassword>",
        ),
        (
            "<db.password>example-password-0000</db.password>",
            "<db.password><SECRET_1></db.password>",
        ),
        (
            '\\"name\\": \\"DB_PASSWORD\\", \\"value\\": \\"example-password-0000\\"',
            '\\"name\\": \\"DB_PASSWORD\\", \\"value\\": \\"<SECRET_1>\\"',
        ),
        (
            "- name: DB_PASSWORD\n  value: 12345678",
            "- name: DB_PASSWORD\n  value: <SECRET_1>",
        ),
        (
            "- name: DB_PASSWORD\n  value: correct horse battery  \n- name: X",
            "- name: DB_PASSWORD\n  value: <SECRET_1>  \n- name: X",
        ),
        (
            "- name: DB_PASSWORD\n  value: pass,word-0000\n- name: X",
            "- name: DB_PASSWORD\n  value: <SECRET_1>\n- name: X",
        ),
        (
            '{"name": "DB_PASSWORD", "value": 12345678}',
            '{"name": "DB_PASSWORD", "value": <SECRET_1>}',
        ),
        (
            '{"name": "DB_PASSWORD", "value": "pass\\"word-0000"}',
            '{"name": "DB_PASSWORD", "value": "<SECRET_1>"}',
        ),
        (
            "<password>pass, word 'here'</password>",
            "<password><SECRET_1></password>",
        ),
        (
            '<password encrypted="false">example-password-0000</password>',
            '<password encrypted="false"><SECRET_1></password>',
        ),
        (
            "<password>\n  example-password-0000\n</password>",
            "<password>\n  <SECRET_1>\n</password>",
        ),
        (
            '<property name="hibernate.connection.password">'
            "example-password-0000</property>",
            '<property name="hibernate.connection.password"><SECRET_1></property>',
        ),
        (
            '<entry key="db.password">example-password-0000</entry>',
            '<entry key="db.password"><SECRET_1></entry>',
        ),
        (
            "aws ssm put-parameter --name /prod/db/password"
            " --value example-password-0000 --type SecureString",
            "aws ssm put-parameter --name /prod/db/password"
            " --value <SECRET_1> --type SecureString",
        ),
        (
            "- {name: DB_PASSWORD, value: 12345678}",
            "- {name: DB_PASSWORD, value: <SECRET_1>}",
        ),
        (
            '[{"name": "DB_PASSWORD", "value": 12345678}]',
            '[{"name": "DB_PASSWORD", "value": <SECRET_1>}]',
        ),
    ],
)
def test_element_and_pair_forms_mask_exactly_the_value(
    text: str, expected: str
) -> None:
    assert evaluate(text).redacted_text == expected


@pytest.mark.parametrize(
    "text",
    [
        _K8S_SECRET_REF,
        "<maxTokens>4096</maxTokens>",
        "<tokenType>Bearer</tokenType>",
        "<password>${DB_PASSWORD}</password>",
        "- name: TOKEN_TTL\n  value: 3600",
        "- name: DB_PASSWORD\n  value:\n- name: LOG_LEVEL\n  value: verbose-mode",
        "- name: SESSION_TOKEN_TTL\n  value: '604800'",
        '{"name": "LOG_LEVEL", "value": "verbose-mode"}',
        'variable "region" {\n  default = "eu-central-1"\n}',
        'resource "aws_ssm_parameter" "db" {\n  name  = "/prod/db/password"\n'
        '  type  = "SecureString"\n  value = var.db_password\n}',
        "aws ssm get-parameter --name /prod/db/password --with-decryption",
        '<small id="passwordHelpBlock">Your password must be 8-20 characters</small>',
    ],
)
def test_element_and_pair_forms_without_a_secret_stay_quiet(text: str) -> None:
    assert evaluate(text).counts == ()


@pytest.mark.parametrize(
    "password",
    [
        "Lm2pQr7z>Kx9w",
        "ab>cdefghijk",
        "Lm2pQr7z]Kx9w",
        "abcdef}ghij",
        "ab}cdefgh",
        "it's-a-secret-pw",
        'pass"word-0000',
        "abc>defgh123",
    ],
)
def test_a_plain_paired_value_is_masked_to_its_line_end(password: str) -> None:
    text = f"env:\n  - name: DB_PASSWORD\n    value: {password}\n  - name: X\n"

    assert evaluate(text).redacted_text == text.replace(password, "<SECRET_1>")


@pytest.mark.parametrize("seed", [5, 6])
def test_a_random_plain_password_in_an_env_list_is_never_partly_exposed(
    seed: int,
) -> None:
    """A YAML plain value starts with a letter here, has no space, and may hold
    every mark a closing bracket or a quote would be."""
    symbols = "!#$&()*+-./:;<=>?@[]^_`{|}~\"'"
    alphabet = string.ascii_letters + string.digits + symbols
    generator = random.Random(seed)
    passwords = [
        generator.choice(string.ascii_letters)
        + "".join(generator.choice(alphabet) for _ in range(15))
        for _ in range(2_000)
    ]

    exposed = [
        password
        for password in passwords
        if evaluate(f"- name: DB_PASSWORD\n  value: {password}").redacted_text
        != "- name: DB_PASSWORD\n  value: <SECRET_1>"
    ]

    assert exposed == []


@pytest.mark.parametrize(
    "text",
    [
        "DB_PASSWORD=12345678",
        "REDIS_PASSWORD=12345678",
        "SMTP_PASS=12345678",
        "PASS=123456",
        "ADMIN_PWD=12345678",
        "CLIENT_SECRET=12345678",
        "dbPassword: 12345678",
        '"dbPassword": "12345678"',
        "DB_ŞİFRE=12345678",
        "db_parola=12345678",
        '<add key="DbPassword" value="12345678"/>',
    ],
)
def test_digits_under_a_password_word_are_a_secret(text: str) -> None:
    assert evaluate(text).counts == (("SECRET", 1),)


def test_one_digit_password_repeated_under_several_keys_is_masked_on_every_line() -> (
    None
):
    text = (
        "POSTGRES_PASSWORD=12345678\nDB_PASSWORD=12345678\n"
        "REDIS_PASSWORD=12345678\nSMTP_PASS=12345678\n"
    )

    assert "12345678" not in evaluate(text).redacted_text


@pytest.mark.parametrize(
    "text",
    [
        "REDIS_PASSWORD=exAMple.SYNthetic",
        "DB_PASSWORD=EXample.synTHetic.nX",
        "DB_PASSWORD=eXAmple.sYNthetic_0000",
        "SMTP_PASSWORD=~/ex&am>>PLE*0000",
        "DB_PASSWORD=/EXa|mp!LE.Syn",
        "DB_PASSWORD=./Ex9.aM",
        "DB_PASSWORD=/var/lib/x",
    ],
)
def test_a_password_that_resembles_code_or_a_path_is_masked(text: str) -> None:
    assert evaluate(text).counts == (("SECRET", 1),)


@pytest.mark.parametrize(
    "text",
    [
        "DB_PASSWORD = config.db_password",
        "smtp_password = settings.SMTP_PASSWORD",
        "const dbPassword = this.dbPassword;",
        'db_password = settings["DB_PASSWORD"]',
        "DB_PASSWORD=${DB_PASS}",
        "DB_PASSWORD=$DB_PASS",
        "DB_PASSWORD=%DB_PASS%",
        "DB_PASSWORD_FILE=/run/secrets/db_password",
        "TOKEN_PATH=~/keys/token.txt",
    ],
)
def test_code_and_references_under_a_password_key_stay_quiet(text: str) -> None:
    assert evaluate(text).counts == ()


@pytest.mark.parametrize(
    "text",
    [
        "PGPASSWORD" * 9_900,
        "pgpassword" * 9_900,
        "PGPASSWORD=" * 9_000,
        "<password>" * 9_900,
        "<password>" + "a" * 99_000,
        "- name: DB_PASSWORD\n" * 4_900,
        '"Name": "/db/password", ' * 4_000,
        "password: str = " * 6_000,
        "password: " + "Optional[" * 11_000,
        'variable "db_password" ' * 4_000,
        '<entry key="password">' * 4_000,
        "<password " + "a" * 99_000,
        "password: " + "a=" * 30_000,
        "--name /db/password " * 4_000,
        "- name: DB_PASSWORD\n  value: " + "}" * 60_000,
    ],
    ids=[
        "glued-upper",
        "glued-lower",
        "glued-assignments",
        "elements",
        "unclosed-element",
        "k8s-names",
        "ssm-names",
        "annotations",
        "nested-annotation",
        "terraform-names",
        "named-elements",
        "unclosed-tag",
        "annotation-equals",
        "cli-names",
        "closing-marks",
    ],
)
def test_a_long_run_of_key_like_text_stays_fast(text: str) -> None:
    started = time.process_time()
    evaluate(text)

    assert time.process_time() - started < 1
