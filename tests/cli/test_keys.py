from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shim_cli.cli.app import app
from shim_cli.cli.keys import parse

runner = CliRunner()
ENV = """\
# synthetic configuration for tests
APP_ENV=staging-zz
export DATABASE_URL=postgresql://kasa_app:synthetic-pw-0000@db.example.internal:5432/kasa
REDIS_URL = redis://localhost:6379/0
AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
STRIPE_SECRET_KEY="sk_test_0123456789abcdefSYNTH"
SENTRY_DSN='https://0123456789abcdef@o1.ingest.example.com/1'
OPS_ALERT_EMAIL=ops@example.com # who gets paged

QUOTED="quoted # not a remark"
FEATURE_FLAG_X=
EMPTY_QUOTES=""
API_BASE=${OTHER_VAR}
just words
DB_PASSWORD="p#ss=w'rd-0000"
SIGNING_PEM="-----BEGIN PRIVATE KEY-----
MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsyntheticsyntheticsynth
-----END PRIVATE KEY-----"
TRAILING=after-pem-zz
NOTE_ONLY= # filled in later
"""
CREDENTIALS = """\
[default]
aws_access_key_id = AKIAIOSFODNN7EXAMPLE
aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY

[prod]
region = eu-west-1
"""
REPORT = """\
.env
  APP_ENV                 set
  DATABASE_URL            set   DB_URI
  REDIS_URL               set
  AWS_ACCESS_KEY_ID       set   SECRET
  STRIPE_SECRET_KEY       set   SECRET
  SENTRY_DSN              set   SECRET
  OPS_ALERT_EMAIL         set   EMAIL
  QUOTED                  set
  FEATURE_FLAG_X          empty
  EMPTY_QUOTES            empty
  API_BASE                ref   (${OTHER_VAR})
  unparsed line 14
  DB_PASSWORD             set   SECRET
  SIGNING_PEM             set   SECRET
  TRAILING                set
  NOTE_ONLY               empty

credentials
  [default]
  aws_access_key_id       set   SECRET
  aws_secret_access_key   set   SECRET
  [prod]
  region                  set
"""


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    (work / ".env").write_text(ENV, encoding="utf-8")
    (work / "credentials").write_text(CREDENTIALS, encoding="utf-8")
    (work / "hostile.env").write_text(HOSTILE, encoding="utf-8")
    (work / "layered.ini").write_text(LAYERED, encoding="utf-8")
    (work / "raw.pem").write_text(PEM, encoding="utf-8")
    (work / "split.env").write_text(SPLIT, encoding="utf-8")
    (work / ".gitconfig").write_text(GITCONFIG, encoding="utf-8")
    for name, text in KEY_FILES.items():
        (work / name).write_text(text, encoding="utf-8")
    monkeypatch.chdir(work)
    return work


HOSTILE = (
    "DB_PASSWORD=$uperSecret2024zz\n"
    "ADMIN_PASSWORD='$Hunter2Synthetic'\n"
    "SIGNING_KEY='-----BEGIN PRIVATE KEY-----\n"
    "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsyntheticsynth0\n"
    "Zm9vYmFyYmF6cXV4c3ludGhldGlj==\n"
    "-----END PRIVATE KEY-----'\n"
    'MESSAGE="he said \\"hi\\"\n'
    "deploy_token=0123456789abcdef-synth\n"
    '[prod-db-hunter2-zz]"\n'
    "FORM=abc\x0cleaked_part=synthetic-zz\n"
    "NODE_KEY=`first-row-zz\n"
    "leaked_word=second-row-zz`\n"
    "ROOT_PASSWORD=#Synthetic-0000\n"
    "MIXED=pre-${HOST}-hidden-zz\n"
    "QUOTED_REF='${NOT_EXPANDED}'\n"
    "AKIAIOSFODNN7EXAMPLE=plain-zz\n"
    "[\x1b[2Jcleared]\n"
    'GREETING="hello\n'
    "AFTER_GREETING=kept-row-zz\n"
    "TLS_KEY=-----BEGIN PRIVATE KEY-----\n"
    "Zm9vYmFyYmF6cXV4c3ludGhldGljMDAwMQ==\n"
    "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAunquotedsynth0\n"
    "-----END PRIVATE KEY-----\n"
    "FLAG=\n"
    "AFTER_PEM=last-row-zz\n"
)
LAYERED = """\
[default]
extra =
    SyntheticCarriedOverValue0123456789abcdef=
    [SyntheticGroupLookingValue]
region = eu-central-1
"""
PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsynthetic0000000\n"
    "SyntheticLastLineOfTheKeyBody0123456789abcdef==\n"
    "-----END PRIVATE KEY-----\n"
)
SPLIT = (
    f'TLS_KEY=\n{PEM}AFTER_KEY=after-key-zz\nOPEN_KEY="{PEM}'
    "-----BEGIN PUBLIC KEY-----MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEsynthetic"
    "-----END PUBLIC KEY-----\n"
    "FLAG=\n"
)
GITCONFIG = (
    "[user]\n"
    "\tname = Synthetic Person\n"
    "\temail = synthetic@example.com\n"
    "\tsigningkey = ABCDEF0123456789\n"
    "[github]\n"
    "\tuser = synthbot\n"
    "\ttoken = ghp_0123456789abcdefghijklmnopqrstuvwxyzAB\n"
    "\tnote = first-row-zz\n"
    "\t\tcontinued-row-zz\n"
)
BODY = (
    "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsynthetic0000000\n"
    "SyntheticLastLineOfTheKeyBody0123456789abcdef==\n"
)
KEY_FILES = {
    "rotated.env": (
        f'#TLS_KEY="-----BEGIN PRIVATE KEY-----\n{BODY}-----END PRIVATE KEY-----"\n'
        'TLS_KEY="-----BEGIN PRIVATE KEY-----\n'
        "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAnewsynthetic00000\n"
        "NewSyntheticLastLine0123456789abcdef==\n"
        '-----END PRIVATE KEY-----"\n'
    ),
    "secrets.toml": (
        "[connections.snowflake]\n"
        '    user = "synthbot"\n'
        f'    private_key = """-----BEGIN PRIVATE KEY-----\n{BODY}'
        '-----END PRIVATE KEY-----"""\n'
    ),
    "bom.pem": f"\ufeff-----BEGIN PRIVATE KEY-----\n{BODY}-----END PRIVATE KEY-----\n",
    "app.properties": (
        "tls.key=-----BEGIN PRIVATE KEY-----\\\n"
        "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsynthetic0000000\\\n"
        "SyntheticLastLineOfTheKeyBody0123456789abcdef==\\\n"
        "-----END PRIVATE KEY-----\n"
        "server.port=8443\n"
    ),
    "indented.ini": (
        "[tls]\n"
        "\tcert = /etc/ssl/site.crt\n"
        "\t#key = -----BEGIN PRIVATE KEY-----\n"
        "\tMIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsynthetic0000000\n"
        "\tSyntheticLastLineOfTheKeyBody0123456789abcdef==\n"
        "\t-----END PRIVATE KEY-----\n"
        "\tport = 443\n"
    ),
    "url.gitconfig": (
        "[user]\n"
        "\tname = Synthetic Person\n"
        '[url "https://oauth2:ghp_Synthetic0123456789abcdefSYNTHabcdef@github.com/"]\n'
        "\tinsteadOf = gh:\n"
    ),
    "documented.env": (
        "# Paste the key from -----BEGIN PRIVATE KEY----- onwards into TLS_KEY.\n"
        "DB_HOST=db.internal\n"
        "DB_PASSWORD=Synthetic-pass-0000\n"
    ),
    "bundle.pem": (
        "-----BEGIN CERTIFICATE-----\n"
        "MIIBcertSyntheticPublicBody0000000000000000000000000000000000000\n"
        "-----END CERTIFICATE----------BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCSyntheticPrivateBody0000000000000\n"
        "KeyTailSynthetic0123456789abcdef==\n"
        "-----END PRIVATE KEY-----\n"
    ),
    "armored.env": (
        'PGP_KEY="-----BEGIN PGP PRIVATE KEY BLOCK-----\n'
        'Comment: Alice "Ali" Synthetic\n'
        "\n"
        "lQOYBGSyntheticBlockBody000000000000000000000000000000000000000\n"
        "PgpTailSynthetic0000000000000000==\n"
        '-----END PGP PRIVATE KEY BLOCK-----"\n'
        "AFTER_PGP=after-pgp-zz\n"
    ),
    "privatekey": "aB3dEfGh1jKlMn0pQrStUvWxYz2a4c6e8g0i2k4m6o8=\n",
    "mention.env": (
        'TLS_KEY_HELP="Paste the key from -----BEGIN PRIVATE KEY----- onwards"\n'
        'PEM_HEADER="-----BEGIN CERTIFICATE-----"\n'
        "TLS_KEY_FILE=./certs/server.key  # starts with -----BEGIN PRIVATE KEY-----\n"
        "DB_PASSWORD=Synthetic-pass-0000\n"
    ),
    "prose.ini": (
        "[tls]\n"
        "Paste the PEM below, starting at -----BEGIN CERTIFICATE-----\n"
        "cert_path = /etc/tls/site.pem\n"
    ),
    "github-app.env": (
        "GITHUB_APP_ID=123456\n"
        "# The private key, -----BEGIN RSA PRIVATE KEY----- on, with \\n escapes\n"
        'GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\\n'
        "MIIEowIBAAKCAQEAsyntheticBodyLine000000000000000000000000000000\\n"
        'SyntheticTail0000==\\n-----END RSA PRIVATE KEY-----\\n"\n'
        "GITHUB_WEBHOOK_SECRET=Synthetic-hook-0000\n"
    ),
    "fernet.key": "OLTmUuRNp_I3DZ4mDicTZVCko6bQf1wMMy-LEiQIP9I=\n",
    "short.key": "Tx4kL9mQ2bA=\n",
    "armored-below.env": (
        "SIGNING_KEY='\n"
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
        "Comment: Alice's signing key\n"
        "\n"
        "lQOYBGSyntheticBlockBody000000000000000000000000000000000000000\n"
        "Tail0123abcDEF==\n"
        "=AbCd\n"
        "-----END PGP PRIVATE KEY BLOCK-----\n"
        "'\n"
        "AFTER_SIGNING=after-signing-zz\n"
    ),
}
KEY_REPORTS = {
    "rotated.env": "  TLS_KEY                 set   SECRET\n",
    "secrets.toml": (
        "  [connections.snowflake]\n"
        "  user                    set\n"
        "  private_key             set   SECRET\n"
    ),
    "bom.pem": "  unparsed line 1\n",
    "app.properties": (
        "  tls.key                 set   SECRET\n  server.port             set\n"
    ),
    "indented.ini": (
        "  [tls]\n  cert                    set\n  port                    set\n"
    ),
    "url.gitconfig": (
        "  [user]\n"
        "  name                    set\n"
        '  [url "https://<SECRET_1>@github.com/"]\n'
        "  insteadOf               set\n"
    ),
    "documented.env": (
        "  DB_HOST                 set\n  DB_PASSWORD             set   SECRET\n"
    ),
    "bundle.pem": "  unparsed line 1\n",
    "armored.env": "  PGP_KEY                 set\n  AFTER_PGP               set\n",
    "privatekey": "  unparsed line 1\n",
    "mention.env": (
        "  TLS_KEY_HELP            set   SECRET\n"
        "  PEM_HEADER              set\n"
        "  TLS_KEY_FILE            set\n"
        "  DB_PASSWORD             set   SECRET\n"
    ),
    "prose.ini": "  [tls]\n  unparsed line 2\n  cert_path               set\n",
    "github-app.env": (
        "  GITHUB_APP_ID           set\n"
        "  GITHUB_APP_PRIVATE_KEY  set   SECRET\n"
        "  GITHUB_WEBHOOK_SECRET   set   SECRET\n"
    ),
    "fernet.key": "  unparsed line 1\n",
    "short.key": "  unparsed line 1\n",
    "armored-below.env": (
        "  SIGNING_KEY             set\n  unparsed line 9\n  AFTER_SIGNING           set\n"
    ),
}
HOSTILE_REPORT = """\
hostile.env
  DB_PASSWORD             set
  ADMIN_PASSWORD          set
  SIGNING_KEY             set   SECRET
  MESSAGE                 set   SECRET
  FORM                    set
  NODE_KEY                set
  ROOT_PASSWORD           set   SECRET
  MIXED                   set
  QUOTED_REF              set
  AKIAIOSFODNN7EXAMPLE    set
  unparsed line 17
  unparsed line 18
  AFTER_GREETING          set
  TLS_KEY                 set   SECRET
  FLAG                    empty
  AFTER_PEM               set
"""
VALUES = {
    ".env": (
        "staging-zz",
        "postgresql://kasa_app:synthetic-pw-0000@db.example.internal:5432/kasa",
        "redis://localhost:6379/0",
        "AKIAIOSFODNN7EXAMPLE",
        "sk_test_0123456789abcdefSYNTH",
        "https://0123456789abcdef@o1.ingest.example.com/1",
        "ops@example.com",
        "quoted # not a remark",
        "p#ss=w'rd-0000",
        "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsyntheticsyntheticsynth",
        "after-pem-zz",
    ),
    "credentials": (
        "AKIAIOSFODNN7EXAMPLE",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "eu-west-1",
    ),
    "hostile.env": (
        "$uperSecret2024zz",
        "$Hunter2Synthetic",
        "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsyntheticsynth0",
        "Zm9vYmFyYmF6cXV4c3ludGhldGlj==",
        'he said \\"hi\\"',
        "deploy_token=0123456789abcdef-synth",
        "[prod-db-hunter2-zz]",
        "abc\x0cleaked_part=synthetic-zz",
        "first-row-zz",
        "leaked_word=second-row-zz",
        "#Synthetic-0000",
        "pre-${HOST}-hidden-zz",
        "${NOT_EXPANDED}",
        "plain-zz",
        "cleared",
        "hello",
        "kept-row-zz",
        "Zm9vYmFyYmF6cXV4c3ludGhldGljMDAwMQ==",
        "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAunquotedsynth0",
        "last-row-zz",
    ),
    "layered.ini": (
        "SyntheticCarriedOverValue0123456789abcdef=",
        "[SyntheticGroupLookingValue]",
        "eu-central-1",
    ),
    "raw.pem": (
        "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsynthetic0000000",
        "SyntheticLastLineOfTheKeyBody0123456789abcdef==",
    ),
    "split.env": (
        "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsynthetic0000000",
        "SyntheticLastLineOfTheKeyBody0123456789abcdef==",
        "after-key-zz",
        "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEsynthetic",
    ),
    "rotated.env": (
        "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsynthetic0000000",
        "SyntheticLastLineOfTheKeyBody0123456789abcdef==",
        "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAnewsynthetic00000",
        "NewSyntheticLastLine0123456789abcdef==",
    ),
    "secrets.toml": (
        "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsynthetic0000000",
        "SyntheticLastLineOfTheKeyBody0123456789abcdef==",
        "synthbot",
    ),
    "bom.pem": (
        "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsynthetic0000000",
        "SyntheticLastLineOfTheKeyBody0123456789abcdef==",
    ),
    "app.properties": (
        "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsynthetic0000000",
        "SyntheticLastLineOfTheKeyBody0123456789abcdef==",
        "8443",
    ),
    "indented.ini": (
        "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEAsynthetic0000000",
        "SyntheticLastLineOfTheKeyBody0123456789abcdef==",
        "/etc/ssl/site.crt",
    ),
    "url.gitconfig": (
        "Synthetic Person",
        "ghp_Synthetic0123456789abcdefSYNTHabcdef",
    ),
    "documented.env": ("db.internal", "Synthetic-pass-0000"),
    "bundle.pem": (
        "MIIBcertSyntheticPublicBody0000000000000000000000000000000000000",
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCSyntheticPrivateBody0000000000000",
        "KeyTailSynthetic0123456789abcdef==",
    ),
    "armored.env": (
        "lQOYBGSyntheticBlockBody000000000000000000000000000000000000000",
        "PgpTailSynthetic0000000000000000==",
        "after-pgp-zz",
    ),
    "privatekey": ("aB3dEfGh1jKlMn0pQrStUvWxYz2a4c6e8g0i2k4m6o8=",),
    "mention.env": ("./certs/server.key", "Synthetic-pass-0000"),
    "prose.ini": ("/etc/tls/site.pem",),
    "github-app.env": (
        "MIIEowIBAAKCAQEAsyntheticBodyLine000000000000000000000000000000",
        "SyntheticTail0000==",
        "Synthetic-hook-0000",
    ),
    "fernet.key": ("OLTmUuRNp_I3DZ4mDicTZVCko6bQf1wMMy-LEiQIP9I=",),
    "short.key": ("Tx4kL9mQ2bA=",),
    "armored-below.env": (
        "lQOYBGSyntheticBlockBody000000000000000000000000000000000000000",
        "Tail0123abcDEF==",
        "after-signing-zz",
    ),
    ".gitconfig": (
        "Synthetic Person",
        "synthetic@example.com",
        "ABCDEF0123456789",
        "synthbot",
        "ghp_0123456789abcdefghijklmnopqrstuvwxyzAB",
        "first-row-zz",
        "continued-row-zz",
    ),
}


def test_each_variable_is_listed_with_its_state_and_type(folder: Path) -> None:
    result = runner.invoke(app, ["keys", ".env", "credentials"])

    assert result.exit_code == 0
    assert result.stdout == REPORT


def test_a_multi_line_value_is_one_variable_on_its_first_line() -> None:
    [entry] = [entry for entry in parse(ENV) if entry.name == "SIGNING_PEM"]

    assert entry.line == 16
    assert entry.value.startswith("-----BEGIN PRIVATE KEY-----\n")
    assert entry.value.endswith("\n-----END PRIVATE KEY-----")


def test_the_json_rows_carry_name_state_entity_line_and_section(folder: Path) -> None:
    result = runner.invoke(app, ["keys", "credentials", ".env", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["schema_version"] == 1
    assert [file["path"] for file in payload["files"]] == ["credentials", ".env"]
    assert payload["files"][0]["variables"] == [
        {
            "name": "aws_access_key_id",
            "state": "set",
            "entity": "SECRET",
            "line": 2,
            "section": "default",
            "reference": None,
        },
        {
            "name": "aws_secret_access_key",
            "state": "set",
            "entity": "SECRET",
            "line": 3,
            "section": "default",
            "reference": None,
        },
        {
            "name": "region",
            "state": "set",
            "entity": None,
            "line": 6,
            "section": "prod",
            "reference": None,
        },
    ]
    rows = {row["line"]: row for row in payload["files"][1]["variables"]}
    assert rows[13] == {
        "name": "API_BASE",
        "state": "ref",
        "entity": None,
        "line": 13,
        "section": "",
        "reference": "${OTHER_VAR}",
    }
    assert rows[14] == {
        "name": None,
        "state": "unparsed",
        "entity": None,
        "line": 14,
        "section": "",
        "reference": None,
    }


def test_a_file_read_the_way_dotenv_reads_it_shows_no_piece_of_a_value(
    folder: Path,
) -> None:
    result = runner.invoke(app, ["keys", "hostile.env"])

    assert result.exit_code == 0
    assert result.stdout == HOSTILE_REPORT


def test_an_indented_dotenv_line_is_its_own_variable() -> None:
    entries = parse("APP=one\n  INDENTED=two\n")

    assert [entry.name for entry in entries] == ["APP", "INDENTED"]


def test_an_ini_line_that_continues_a_value_is_part_of_it(folder: Path) -> None:
    result = runner.invoke(app, ["keys", "layered.ini"])

    assert result.exit_code == 0
    assert result.stdout == (
        "layered.ini\n"
        "  [default]\n"
        "  extra                   set\n"
        "  region                  set\n"
    )


def test_a_private_key_on_its_own_line_is_one_unparsed_line(folder: Path) -> None:
    result = runner.invoke(app, ["keys", "raw.pem", "split.env"])

    assert result.exit_code == 0
    assert result.stdout == (
        "raw.pem\n"
        "  unparsed line 1\n"
        "\n"
        "split.env\n"
        "  TLS_KEY                 empty\n"
        "  unparsed line 2\n"
        "  AFTER_KEY               set\n"
        "  OPEN_KEY                set   SECRET\n"
        "  unparsed line 11\n"
        "  FLAG                    empty\n"
    )


@pytest.mark.parametrize("name", list(KEY_FILES))
def test_a_key_or_a_credential_in_a_header_never_prints(
    folder: Path, name: str
) -> None:
    result = runner.invoke(app, ["keys", name])

    assert result.exit_code == 0
    assert result.stdout == f"{name}\n{KEY_REPORTS[name]}"


def test_ini_keys_indented_alike_are_each_a_variable(folder: Path) -> None:
    result = runner.invoke(app, ["keys", ".gitconfig"])

    assert result.exit_code == 0
    assert [line.split()[0] for line in result.stdout.splitlines()[1:]] == [
        "[user]",
        "name",
        "email",
        "signingkey",
        "[github]",
        "user",
        "token",
        "note",
    ]


@pytest.mark.parametrize(
    "files",
    (
        [".env", "credentials"],
        ["hostile.env"],
        ["layered.ini"],
        ["raw.pem", "split.env"],
        [".gitconfig"],
        *([name] for name in KEY_FILES),
    ),
)
@pytest.mark.parametrize("json_output", (False, True))
def test_no_value_and_no_four_characters_of_one_are_printed(
    folder: Path, files: list, json_output: bool
) -> None:
    arguments = ["keys", *files, *(["--json"] if json_output else [])]
    output = runner.invoke(app, arguments).output
    values = [value for name in files for value in VALUES[name]]
    pieces = {
        value[start : start + 4] for value in values for start in range(len(value) - 3)
    }

    assert [value for value in values if value in output] == []
    assert sorted(piece for piece in pieces if piece in output) == []


@pytest.mark.parametrize(
    ("name", "sentence"),
    (
        ("folder", "folder is not a regular file"),
        ("missing.env", "missing.env does not exist"),
        ("big.env", "big.env is larger than 1 MB"),
        ("binary.env", "binary.env is not UTF-8 text"),
    ),
)
def test_a_file_shim_will_not_read_exits_2_with_one_sentence(
    folder: Path, name: str, sentence: str
) -> None:
    (folder / "folder").mkdir()
    (folder / "big.env").write_text("KEY=" + "a" * 999_997, encoding="utf-8")
    (folder / "binary.env").write_bytes(b"KEY=\xff\xfe secret-zz\n")

    result = runner.invoke(app, ["keys", ".env", name])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == f"shim: {sentence}.\n"


def test_nothing_is_written_anywhere(folder: Path, tmp_path: Path) -> None:
    before = {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*")}

    result = runner.invoke(app, ["keys", ".env", "credentials"])

    assert result.exit_code == 0
    assert {path: path.stat().st_mtime_ns for path in tmp_path.rglob("*")} == before


def test_a_file_of_exactly_one_megabyte_is_read(folder: Path) -> None:
    (folder / "full.env").write_text("KEY=" + "a" * 999_996, encoding="utf-8")

    result = runner.invoke(app, ["keys", "full.env"])

    assert result.exit_code == 0
    assert result.stdout == "full.env\n  KEY                     set\n"


def test_a_refusal_under_json_is_json(folder: Path) -> None:
    result = runner.invoke(app, ["keys", "missing.env", "--json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"] == "missing.env does not exist"


def test_a_value_the_detector_cannot_read_is_marked_not_inspected(
    folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / "shim.toml"
    settings.write_text("enabled_entities = [\n", encoding="utf-8")
    monkeypatch.setenv("SHIM_CONFIG", str(settings))

    result = runner.invoke(app, ["keys", ".env"])

    assert result.exit_code == 0
    assert "  APP_ENV                 set   not inspected\n" in result.stdout
    assert "  API_BASE                ref   (${OTHER_VAR})\n" in result.stdout
