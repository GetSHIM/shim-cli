"""MCP and unknown tool inputs through the installed hook, under the defaults.

Under the default `enforce`, an MCP or unknown tool's call whose input carries
a finding is refused, so every detector false positive on an ordinary MCP input
is a refused call. `mcp-inputs-clean-v1.json` is ordinary work with no personal
data and no credentials: every call passes untouched and unreported, but for
its `known_false_positives`, which are refused: rescuing one would miss a
credential of the same shape. The companion list holds what must still be
refused, the near misses of each relaxation included. Each run is the hook as Claude Code starts it, in a fresh
home and temporary directory.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from shim_cli.guard import evaluate

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests" / "corpus" / "mcp-inputs-clean-v1.json"
CORPUS_DATA = json.loads(CORPUS.read_text(encoding="utf-8"))
CASES = CORPUS_DATA["cases"]
KNOWN = CORPUS_DATA["known_false_positives"]
COMMAND = (sys.executable, "-I", "-B", "-m", "shim_cli.hook", "claude")


def _hook(tmp_path: Path, tool: str, tool_input: dict, settings: str = ""):
    tmp_path.mkdir(exist_ok=True)
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    home.mkdir()
    temporary.mkdir()
    if settings:
        folder = home / ".config" / "shim"
        folder.mkdir(parents=True)
        (folder / "config.toml").write_text(settings, encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SHIM_", "XDG_", "CLAUDE_"))
    }
    environment.update(HOME=str(home), TMPDIR=str(temporary))
    raw = json.dumps(
        {
            "session_id": "u27",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/work",
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
        }
    ).encode()
    result = subprocess.run(
        COMMAND,
        input=raw,
        capture_output=True,
        cwd=ROOT,
        env=environment,
        check=False,
        timeout=60,
    )
    assert (result.returncode, result.stderr) == (0, b"")
    records = [
        json.loads(line)
        for path in temporary.rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    return result.stdout, records


def test_the_clean_corpus_covers_mcp_and_unknown_tools() -> None:
    tools = {case["tool"] for case in CASES}

    assert len(CASES) == len({case["id"] for case in CASES}) >= 40
    assert all(
        tool.startswith("mcp__") for tool in tools - {"SomeToolANewerClientAdds"}
    )
    assert "SomeToolANewerClientAdds" in tools


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_an_ordinary_mcp_input_is_allowed_unreported(
    tmp_path: Path, case: dict
) -> None:
    stdout, records = _hook(tmp_path, case["tool"], case["tool_input"])

    assert stdout == b""
    assert [
        (record["direction"], record["action"], record["entities"])
        for record in records
    ] == [("outbound", "allow", {})]


def _refusal(tool: str, found: str) -> str:
    return (
        f"shim: blocked {found} in {tool}. shim cannot tell where this tool sends "
        "its input, so it refuses the call rather than rewrite it. Leave these "
        "values out, or ask the user how to continue. The user can allow this "
        f'tool with "{tool}" = "warn" under [mode] in shim\'s settings.'
    )


WRITE = "mcp__filesystem__write_file"
SLACK = "mcp__slack__slack_post_message"
QUERY = "mcp__postgres__query"

# Each relaxation's near miss: a value that sits where code would, but is the
# credential itself.
REFUSED = (
    (
        WRITE,
        {"path": "/w/.env", "content": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"},
        "SECRET",
    ),
    (WRITE, {"path": "/w/.env", "content": "PASSWORD=password\n"}, "SECRET"),
    (
        WRITE,
        {"path": "/w/compose.yaml", "content": "db:\n  password: password\n"},
        "SECRET",
    ),
    (
        WRITE,
        {
            "path": "/w/app.properties",
            "content": "spring.datasource.password=password\n",
        },
        "SECRET",
    ),
    (
        WRITE,
        {"path": "/w/run.sh", "content": "password=password; ./migrate\n"},
        "SECRET",
    ),
    (
        WRITE,
        {"path": "/w/run.sh", "content": "export DB_PASSWORD=s3cr3tPass1; npm start\n"},
        "SECRET",
    ),
    (
        WRITE,
        {
            "path": "/w/run.sh",
            "content": 'DB_PASSWORD="${DB_PASSWORD:-exAMple.SYNthetic}"\n',
        },
        "SECRET",
    ),
    (
        WRITE,
        {"path": "/w/.env", "content": "REDIS_PASSWORD=exAMple.SYNthetic\n"},
        "SECRET",
    ),
    (WRITE, {"path": "/w/.env", "content": "password=Summer(fall)\n"}, "SECRET"),
    (
        WRITE,
        {"path": "/w/a.py", "content": 'connect(host=host, password="s3cr3tPass1")\n'},
        "SECRET",
    ),
    (
        WRITE,
        {"path": "/w/a.py", "content": 'self.password = "s3cr3tPass1"\n'},
        "SECRET",
    ),
    (
        WRITE,
        {"path": "/w/A.java", "content": 'this.password = "s3cr3tPass1";\n'},
        "SECRET",
    ),
    (
        WRITE,
        {"path": "/w/a.cs", "content": 'conn = "Server=db;Password=s3cr3tPass1;";\n'},
        "SECRET",
    ),
    (
        WRITE,
        {
            "path": "/w/k.pem",
            "content": "-----BEGIN PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END PRIVATE KEY-----\n",
        },
        "SECRET",
    ),
    (
        SLACK,
        {"channel_id": "C0123456789", "text": "the staging password s3cr3tPass1."},
        "SECRET",
    ),
    (
        SLACK,
        {
            "channel_id": "C0123456789",
            "text": "token: ghp_0123456789abcdef0123456789abcdef0123",
        },
        "SECRET",
    ),
    (
        SLACK,
        {
            "channel_id": "C0123456789",
            "text": "Ping jane.doe@example.com about the export",
        },
        "EMAIL",
    ),
    (
        SLACK,
        {"channel_id": "C0123456789", "text": "Call +90 532 123 45 67 after 14:00"},
        "PHONE",
    ),
    (
        SLACK,
        {"channel_id": "C0123456789", "text": "The service listens on 203.0.113.7"},
        "IP_ADDRESS",
    ),
    (
        SLACK,
        {"channel_id": "C0123456789", "text": "refund to TR330006100519786457841326"},
        "IBAN",
    ),
    (
        SLACK,
        {"channel_id": "C0123456789", "text": "card 4111 1111 1111 1111 declined"},
        "CREDIT_CARD",
    ),
    (
        QUERY,
        {"sql": "SELECT id FROM accounts WHERE email = 'jane.doe@example.com'"},
        "EMAIL",
    ),
    (
        QUERY,
        {"sql": "SELECT id FROM accounts WHERE tckn = '10000000146'"},
        "TR_NATIONAL_ID",
    ),
    (
        "mcp__fetch__fetch",
        {"url": "postgresql://app:s3cr3tPass1@db:5432/app"},
        "DB_URI",
    ),
    ("SomeToolANewerClientAdds", {"note": "APP_PASSWORD=s3cr3tPass1"}, "SECRET"),
    (WRITE, {"path": "/w/a.cfg", "content": "cfg.token = Zq8Lm3Np7Rt1;\n"}, "SECRET"),
    (
        WRITE,
        {"path": "/w/a.cfg", "content": "mail.smtp.password = letmein;\n"},
        "SECRET",
    ),
    (
        SLACK,
        {
            "channel_id": "C0123456789",
            "text": "Set spring.datasource.password=Welcome1; and redeploy",
        },
        "SECRET",
    ),
    (WRITE, {"path": "/w/.env", "content": "DB_PASSWORD=Hello.World(1)\n"}, "SECRET"),
    (WRITE, {"path": "/w/a.py", "content": "password = x.y(Xk9mP2qL7wZt)\n"}, "SECRET"),
    (WRITE, {"path": "/w/a.py", "content": "password = Xk9mP2.qL7wZt()\n"}, "SECRET"),
    (
        WRITE,
        {"path": "/w/a.yaml", "content": "db: {user: admin, password: password}\n"},
        "SECRET",
    ),
    (WRITE, {"path": "/w/.env", "content": "PASSWORD=Password,extra\n"}, "SECRET"),
    (
        WRITE,
        {
            "path": "/w/application.properties",
            "content": "spring.datasource.password = adminPassword\n",
        },
        "SECRET",
    ),
    (
        WRITE,
        {
            "path": "/w/application.properties",
            "content": "spring.datasource.password = password\n",
        },
        "SECRET",
    ),
    (
        WRITE,
        {
            "path": "/w/application.properties",
            "content": "mail.smtp.password = companyPassword\n",
        },
        "SECRET",
    ),
    (
        WRITE,
        {"path": "/w/.env", "content": "DB_PASSWORD=summer2024.winter.go(1)\n"},
        "SECRET",
    ),
)


# Each code shape's near miss in the detector itself, as every event reads it:
# an unquoted credential where a member, a call, an argument or a handed-on
# variable would sit.
@pytest.mark.parametrize(
    ("text", "value"),
    (
        ("cfg.token = Zq8Lm3Np7Rt1;", "Zq8Lm3Np7Rt1"),
        ("db.password=P4ssw0rd;", "P4ssw0rd"),
        ("cfg.api_key=Zq8Lm3Np7Rt1;", "Zq8Lm3Np7Rt1"),
        ("ds.secret=Zq8Lm3Np7Rt1;", "Zq8Lm3Np7Rt1"),
        ("java -Dspring.datasource.password=P4ssw0rd; java -jar app.jar", "P4ssw0rd"),
        ("Set spring.datasource.password=Welcome1; and redeploy", "Welcome1"),
        ("java -Ddb.password=Xk9mP2qL7wZt; ./run", "Xk9mP2qL7wZt"),
        ("mail.smtp.password = Xk9mP2qL7wZt;", "Xk9mP2qL7wZt"),
        ("mail.smtp.password = letmein;", "letmein"),
        (
            "jdbc:sqlserver://h;databaseName=app;user.password=Xk9mP2qL7wZt;",
            "Xk9mP2qL7wZt",
        ),
        ("app.token = a1b2c3d4e5f6a7b8c9d0;", "a1b2c3d4e5f6a7b8c9d0"),
        ("settings.secret_key = f8a9b0c1d2e3f4a5b6c7d8e9;", "f8a9b0c1d2e3f4a5b6c7d8e9"),
        ("config.api_key = sk9Xk9mP2qL7wZtQ8;", "sk9Xk9mP2qL7wZtQ8"),
        ("obj.password = hunter2abc;", "hunter2abc"),
        ("DB_PASSWORD=Hello.World(1)", "Hello.World(1)"),
        ("PASSWORD=Hello.World(1)", "Hello.World(1)"),
        ("DB_PASSWORD: Blue.Harbor(7)", "Blue.Harbor(7)"),
        ("password: Spring.Time()", "Spring.Time()"),
        ("api_key=Admin::Pass(1)", "Admin::Pass(1)"),
        ("password=Winter.x(2024)", "Winter.x(2024)"),
        ("SMTP_PASSWORD=mail.send(Xk9mP2qL7wZt)", "Xk9mP2qL7wZt"),
        ("DB_PASSWORD=pg.connect(a1b2c3d4e5f6a7b8)", "a1b2c3d4e5f6a7b8"),
        ("token = auth.login(a1b2c3d4e5f6a7b8c9d0)", "a1b2c3d4e5f6a7b8c9d0"),
        ("api_key = client.with_key(sk9Xk9mP2qL7wZtQ8)", "sk9Xk9mP2qL7wZtQ8"),
        ("password = x.y(Xk9mP2qL7wZt)", "Xk9mP2qL7wZt"),
        ("password = Xk9mP2.qL7wZt()", "Xk9mP2"),
        ("{user: admin, password: password}", "password}"),
        ("password=Password,", "Password"),
        ("PASSWORD=Password,extra", "Password"),
        ("PASSWORD=Password)", "Password"),
        ("token = Token)", "Token"),
        # A .properties line's unquoted value is the password itself.
        ("spring.datasource.password = adminPassword", "adminPassword"),
        (
            "spring.datasource.password = myPassword\nspring.datasource.url = jdbc:x",
            "myPassword",
        ),
        ("mail.smtp.password = companyPassword", "companyPassword"),
        ("app.secret = mySecret", "mySecret"),
        ("app.api_key = myApiKey", "myApiKey"),
        ("oauth.client.secret = clientSecret", "clientSecret"),
        ("db.password = adminPassword # prod", "adminPassword"),
        ("x.password = password", "= password"),
        ("x.password = password\n", "= password"),
        # An environment lookup only as its language spells it.
        ("api_key = Os.Getenv(Xk9mP2qL7wZt)", "Xk9mP2qL7wZt"),
        ("token = System.getenvs(Xk9mP2qL7wZt)", "Xk9mP2qL7wZt"),
        # A value holding a digit is never read as code: a passphrase.
        ("export PASSWORD=pass1.pass2.go(1)", "pass1.pass2.go(1)"),
        ("DB_PASSWORD=summer2024.winter.go(1)", "summer2024.winter.go(1)"),
        ("token=ab12.cd34.ef(5)", "ab12.cd34.ef(5)"),
        ("secret=x1.y2.z(9)", "x1.y2.z(9)"),
        ("token=x1::y2::z3(4)", "x1::y2::z3(4)"),
    ),
)
def test_an_unquoted_credential_shaped_like_code_is_still_a_secret(
    text: str, value: str
) -> None:
    decision = evaluate(text)

    assert decision.counts == (("SECRET", 1),)
    assert value not in decision.redacted_text


# A passphrase written like a dotted call, generated: the base masked each of
# these whatever the call's argument, so reading one as code is a new miss. A
# digit keeps any key's value a secret; under a bare key word, a member's
# included, a value written in names stays one too.
_ANY_KEY = (
    "DB_PASSWORD=",
    "DB_PASSWORD: ",
    "export SMTP_PASSWORD=",
    "CLIENT_SECRET=",
    "API_TOKEN=",
    "auth_token: ",
    "şifre=",
)
_BARE_KEY = (
    "PASSWORD=",
    "password=",
    "password = ",
    "password: ",
    "--password=",
    "spring.datasource.password = ",
    "spring.datasource.password=",
    "db.password=",
    "x.password = ",
    "self.password = ",
    "secret=",
    "app.secret = ",
    "token=",
    "this.token = ",
    "api_key = ",
    "API_KEY=",
    "app.api_key = ",
)
_DIGIT_PATH = (
    "summer2024.winter.go",
    "pass1.pass2.go",
    "ab12.cd34.ef",
    "x1.y2.z",
    "x1::y2::z3",
    "hunter2.login",
    "admin1.Pass",
    "Winter2024.x",
    "p4ss.word",
)
_NAME_PATH = (
    "Blue.Harbor",
    "Winter.x",
    "Spring.Time",
    "Admin::Pass",
    "Hello.World",
    "pass.word",
    "correct.horse.battery",
    "summer.winter.go",
)
_CALL = (
    "",
    "()",
    "(1)",
    "(2024)",
    "(x)",
    "(a.b)",
    "(a_b)",
    "[0]",
    "[x]",
    '("x")',
    "('x')",
    '("',
    "(f(1))",
    "(g())",
    "(a.b(1))",
    "().go(1)",
    "(1)[0]",
    "(x).run()",
    "()[0]",
    "(1))",
    "(), ",
    "(g(), x)",
    "(x, 1)",
)


def test_a_passphrase_shaped_like_a_call_is_masked_whatever_its_argument() -> None:
    missed = []
    for keys, paths in ((_ANY_KEY + _BARE_KEY, _DIGIT_PATH), (_BARE_KEY, _NAME_PATH)):
        for key, path, call, end in itertools.product(
            keys, paths, _CALL, ("", "\n", ";", " # prod")
        ):
            text = key + path + call + end
            start, stop = len(key), len(key + path + call)
            if not any(
                found.entity_type == "SECRET"
                and found.start < stop
                and found.end > start
                for found in evaluate(text).findings
            ):
                missed.append(text)

    assert missed == []


# An environment lookup is no secret; the literal it falls back to, or has
# added to it, is one when the variable it reads or the key it is assigned to
# names a secret. Generated: each lookup and fallback operator, under each
# assignment, for each secret.
_FALLBACK = (
    'os.getenv("{name}", "{secret}")',
    "getenv('{name}', '{secret}')",
    'os.environ.get("{name}", "{secret}")',
    'os.environ.get("{name}") or "{secret}"',
    'process.env.{name} || "{secret}"',
    'process.env["{name}"] ?? "{secret}"',
    "import.meta.env.{name} || '{secret}'",
    'os.Getenv("{name}") + "{secret}"',
    'System.getenv().getOrDefault("{name}", "{secret}")',
    'System.getenv("{name}") ?: "{secret}"',
    'Environment.GetEnvironmentVariable("{name}") ?? "{secret}"',
    'env::var("{name}").unwrap_or("{secret}".to_string())',
    'env::var("{name}").unwrap_or_else(|_| "{secret}".to_string())',
    'std::env::var("{name}").unwrap_or_else(|_e| String::from("{secret}"))',
    'env::var("{name}").unwrap_or(String::from("{secret}"))',
    'std::env::var("{name}").ok().unwrap_or_else(|| "{secret}".into())',
    "os.getenv('{name}', default='{secret}')",
    'os.getenv(key="{name}", default="{secret}")',
    'os.getenv("{name}",\n    "{secret}")',
    "process.env.{name} ?? `{secret}`",
    "getenv('{name}') ?: '{secret}'",
    "$_ENV['{name}'] ?? '{secret}'",
    'ENV.fetch("{name}", "{secret}")',
    'ENV["{name}"] || "{secret}"',
    "${{{name}:?}}{secret}",
    "${{{name}}}{secret}",
    "${{{name}:-${{FALLBACK}}}}{secret}",
    "${{{name}:=${{FALLBACK}}}}{secret}",
)
# A call wrapping the lookup may take anything: only a secret's name counts.
_WRAPPED = ('Optional.ofNullable(System.getenv("{name}")).orElse("{secret}")',)
_ASSIGNED = (
    "",
    "password = ",
    "String password = ",
    "const apiKey = ",
    "token := ",
    "DB_PASSWORD=",
    "  secret: ",
)


def test_a_literal_an_environment_lookup_falls_back_to_is_masked() -> None:
    missed = [
        text
        for assigned, form, name, secret in itertools.product(
            _ASSIGNED,
            _FALLBACK + _WRAPPED,
            ("DB_PASSWORD", "API_TOKEN", "SECRET_KEY", "APP_SETTING"),
            ("Hunter2xyz!", "Zq8Lm3Np7Rt1"),
        )
        if name != "APP_SETTING" or (assigned and form not in _WRAPPED)
        for text in (assigned + form.format(name=name, secret=secret) + ";",)
        if secret in evaluate(text).redacted_text
    ]

    assert missed == []


@pytest.mark.parametrize(
    "text",
    (
        'secret_key: str = os.environ.get("SECRET_KEY", "")',
        'apiKey: process.env.API_KEY ?? "",',
        "const token = process.env.API_TOKEN ?? null;",
        "${APP_TOKEN:-${FALLBACK_TOKEN}}",
        'port = os.getenv("PORT", "8080")',
        'base = process.env.API_BASE_URL || "https://api.example.invalid/v2"',
        'path = os.getenv("DB_PASSWORD_FILE", "/run/secrets/db")',
        'token = os.getenv("API_TOKEN", "<your-api-token>")',
        'region = System.getenv().getOrDefault("AWS_REGION", "eu-west-1");',
        "HOME_DIR=${HOME}/workspace",
        "DATABASE_URL=postgres://${DB_USER}:${DB_PASSWORD}@db.internal/app",
        "${API_TOKEN:-${FALLBACK_TOKEN}}",
        # A name a variable starts, and a tail of letters alone, are no literal.
        "aws secretsmanager get-secret-value --secret-id ${SECRET_PREFIX}-database",
        "docker tag app ${REGISTRY}/app:${GIT_TOKEN_SHA}-build",
        "cp app.key ${SECRET_KEY}_backup_file",
        "TAG=${API_TOKEN_VERSION}release",
        # A JavaScript template literal is code.
        "const token = process.env.API_TOKEN ?? `${prefix}-default`;",
        # The next argument or entry after a lookup is not its default.
        'headers = {"X-Api-Key": os.getenv("API_KEY"), "Accept": "application/json"}',
        'args = [os.getenv("API_TOKEN"), "--verbose-mode"]',
        'assert os.getenv("API_TOKEN"), "API_TOKEN is required"',
        'conn = connect(os.getenv("DB_PASSWORD"), "dbname=app")',
        'client = make_client(os.getenv("API_TOKEN"),\n    "application/json")',
        'client = Client(token=os.getenv("API_TOKEN"),\n    default="fallback-v1")',
        'client := NewClient(os.Getenv("API_TOKEN"), "eu-central-1")',
        'Client(System.getenv("API_TOKEN"), "application/json");',
        'Client.new(ENV["API_KEY"], "application/json")',
        "$client = new Client($_ENV['API_KEY'], 'application/json');",
        'fetch(url, { headers: { Authorization: process.env.API_TOKEN, "Content-Type": "application/json" } });',
    ),
)
def test_a_lookup_that_falls_back_to_no_credential_stays_quiet(text: str) -> None:
    assert evaluate(text).findings == ()


@pytest.mark.parametrize(
    ("text", "literal"),
    (
        # Another variable's default, inside a call the key is assigned from.
        (
            'password = hash_password(os.getenv("DEFAULT_ROLE", "readonly-user1"))',
            "readonly-user1",
        ),
        # A message after the lookup's call closed.
        (
            'String password = Objects.requireNonNull(System.getenv("DB_PASSWORD"), "DB_PASSWORD must be set");',
            "DB_PASSWORD must be set",
        ),
        (
            'String token = requireEnv(System.getenv("API_TOKEN"), "API_TOKEN-missing");',
            "API_TOKEN-missing",
        ),
        ('token = sign(os.getenv("SECRET_KEY"), "HS256-payload")', "HS256-payload"),
    ),
)
def test_a_literal_that_is_no_default_of_the_lookup_is_left(
    text: str, literal: str
) -> None:
    assert literal in evaluate(text).redacted_text


@pytest.mark.parametrize(
    ("lookup", "assignment"),
    (
        (
            'token = cache.get(os.getenv("TOKEN_CACHE_KEY", "session:token:v1"))',
            "TOKEN_CACHE_KEY=session:token:v1",
        ),
        (
            'api_key = str(os.getenv("API_KEY_VERSION", "v2-2024"))',
            "API_KEY_VERSION=v2-2024",
        ),
        ('x = os.getenv("JWT_SECRET", "change-me-1")', "JWT_SECRET=change-me-1"),
    ),
)
def test_a_default_under_a_secret_name_is_read_as_its_assignment_is(
    lookup: str, assignment: str
) -> None:
    literal = assignment.split("=", 1)[1]
    assert (literal in evaluate(lookup).redacted_text) == (
        literal in evaluate(assignment).redacted_text
    )


@pytest.mark.parametrize(("tool", "tool_input", "entity"), REFUSED)
def test_a_credential_or_personal_data_in_an_mcp_input_is_still_refused(
    tmp_path: Path, tool: str, tool_input: dict, entity: str
) -> None:
    stdout, [record] = _hook(tmp_path, tool, tool_input)

    assert json.loads(stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _refusal(tool, f"{entity} (1)"),
        }
    }
    assert (record["action"], record["entities"]) == ("deny", {entity: 1})


@pytest.mark.parametrize("case", KNOWN, ids=[case["id"] for case in KNOWN])
def test_a_known_false_positive_is_refused(tmp_path: Path, case: dict) -> None:
    stdout, [record] = _hook(tmp_path, case["tool"], case["tool_input"])

    reason = json.loads(stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == _refusal(case["tool"], "SECRET (1)")
    assert (record["action"], record["entities"]) == ("deny", {"SECRET": 1})


@pytest.mark.parametrize("tool", (SLACK, "SomeToolANewerClientAdds"))
def test_the_key_the_refusal_names_allows_that_tool(tmp_path: Path, tool: str) -> None:
    tool_input = {"text": "Ping jane.doe@example.com about the export"}

    stdout, [record] = _hook(
        tmp_path, tool, tool_input, settings=f'[mode]\n"{tool}" = "warn"\n'
    )

    assert json.loads(stdout) == {
        "systemMessage": f"shim: found EMAIL (1) in {tool}. Not modified."
    }
    assert (record["mode"], record["action"]) == ("warn", "report")


def test_a_refusal_for_a_tool_whose_name_is_scrubbed_names_no_key(
    tmp_path: Path,
) -> None:
    tool = "mcp__crm__jane.doe@example.com"
    tool_input = {"text": "Ping jane.doe@example.com about the export"}

    stdout, [record] = _hook(tmp_path, tool, tool_input)

    assert json.loads(stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "shim: blocked EMAIL (1) in <EMAIL_1>. shim cannot tell where this "
                "tool sends its input, so it refuses the call rather than rewrite "
                "it. Leave these values out, or ask the user how to continue."
            ),
        }
    }
    assert (record["action"], record["tool_name"]) == ("deny", "<EMAIL_1>")


@pytest.mark.parametrize("tool", ("mcp__dosya__yaz_ş", "mcp__x__😀", 'mcp__x__"q"'))
def test_the_line_the_refusal_suggests_is_valid_settings(
    tmp_path: Path, tool: str
) -> None:
    tool_input = {"text": "Ping jane.doe@example.com about the export"}
    stdout, _ = _hook(tmp_path / "refused", tool, tool_input)
    reason = json.loads(stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    line = reason.split("allow this tool with ", 1)[1].split(" under [mode]", 1)[0]

    stdout, [record] = _hook(
        tmp_path / "allowed", tool, tool_input, settings=f"[mode]\n{line}\n"
    )

    assert json.loads(stdout) == {
        "systemMessage": f"shim: found EMAIL (1) in {tool}. Not modified."
    }
    assert (record["mode"], record["action"]) == ("warn", "report")
