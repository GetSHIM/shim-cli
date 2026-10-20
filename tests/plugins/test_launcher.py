from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from functools import partial
from pathlib import Path

import pytest

from shim_cli.hook import _DENIED_CALL, _PROMPT_EVENTS, _error_output

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "shim-cli"
LAUNCHER = PLUGIN / "hooks" / "run-shim"
BUILDER = ROOT / "scripts" / "build_zipapp.py"
COMMITTED = PLUGIN / "bin" / "shim.pyz"
MAX_ARCHIVE_BYTES = 500_000
CLIENTS = ("claude", "codex", "copilot")
# Every client whose prompt the launcher must be able to withhold on its own.
PROMPT_CLIENTS = (*CLIENTS, "vscode")
TOOL_CLIENTS = ("claude", "vscode")
# The archive guard repeats these names; every one of them must withhold.
PROMPT_EVENTS = sorted(_PROMPT_EVENTS)
SECRET_PROMPT = "Contact alice@example.com"
TOOL_EVENT = json.dumps(
    {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": ".env"},
        "tool_response": {"content": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"},
    }
).encode()
TOOL_CALL = json.dumps(
    {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo AKIAIOSFODNN7EXAMPLE"},
    }
).encode()


def _payload(client: str, prompt: str = SECRET_PROMPT) -> bytes:
    if client == "copilot":
        return json.dumps({"prompt": prompt, "transformedPrompt": prompt}).encode()
    return json.dumps(
        {"hook_event_name": "UserPromptSubmit", "prompt": prompt}
    ).encode()


def _answered_uninspected(result, reason: str, call: bool, stderr: bytes = b""):
    """A tool call nothing could inspect is denied with the hook's own answer;
    any other tool event is told it was not inspected."""
    assert result.returncode == 0
    assert b"AKIAIOSFODNN7EXAMPLE" not in result.stdout + result.stderr
    if call:
        assert result.stdout == _DENIED_CALL
        assert result.stderr == stderr + (
            f"shim: {reason}; the tool call was not allowed.\n".encode()
        )
    else:
        assert result.stderr == stderr
        assert _uninspected(result.stdout) == (
            f"shim: {reason}; this event was not inspected."
        )


def _uninspected(output: bytes) -> str:
    """The report a tool event gets when nothing could inspect it."""
    document = json.loads(output)
    assert list(document) == ["systemMessage"]
    message = document["systemMessage"]
    assert message.startswith("shim: ")
    assert message.endswith("; this event was not inspected.")
    return message


@pytest.fixture(scope="session")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("zipapp") / "shim.pyz"
    subprocess.run(
        (sys.executable, str(BUILDER), "--output", str(target)),
        capture_output=True,
        check=True,
        env=os.environ | {"TZ": "UTC"},
        preexec_fn=partial(os.umask, 0o022),
        timeout=300,
    )
    return target


def _pin_settings(environment: dict[str, str]) -> None:
    """A SHIM_CONFIG that names a missing file is refused, so the file exists."""
    if named := environment.get("SHIM_CONFIG"):
        Path(named).touch(mode=0o600)


def _run(
    client: str,
    environment: dict[str, str],
    prompt: str = SECRET_PROMPT,
    cwd: Path | None = None,
):
    """Run the launcher for a prompt event, the way every hook file calls it."""
    _pin_settings(environment)
    return subprocess.run(
        (str(LAUNCHER), client, "", "prompt"),
        input=_payload(client, prompt),
        capture_output=True,
        check=False,
        cwd=cwd,
        env=environment,
        timeout=120,
    )


def _run_tool_event(client: str, environment: dict[str, str], call: bool = False):
    """Run the launcher for a tool event; a call is marked, as the hook files
    mark PreToolUse."""
    _pin_settings(environment)
    return subprocess.run(
        (str(LAUNCHER), client, "", "call") if call else (str(LAUNCHER), client),
        input=TOOL_CALL if call else TOOL_EVENT,
        capture_output=True,
        check=False,
        env=environment,
        timeout=120,
    )


def _python_path(root: Path) -> str:
    (root / f"python{sys.version_info.major}.{sys.version_info.minor}").symlink_to(
        sys.executable
    )
    return str(root)


def _plugin(tmp_path: Path, archive: Path) -> Path:
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "shim.pyz").write_bytes(archive.read_bytes())
    return root


def _isolated(tmp_path: Path, **environment: str) -> dict[str, str]:
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    home.mkdir(exist_ok=True)
    temporary.mkdir(exist_ok=True)
    return {"HOME": str(home), "TMPDIR": str(temporary), **environment}


@pytest.mark.parametrize("client", PROMPT_CLIENTS)
def test_launcher_withholds_the_prompt_when_nothing_is_runnable(
    client: str, tmp_path: Path
) -> None:
    result = _run(client, _isolated(tmp_path, PATH="/nonexistent"))

    assert result.returncode == 0
    assert result.stdout == _error_output(client), "the hook's own generic block"
    assert result.stderr == (
        b"shim: no hook available. Install with 'uv tool install shim' or "
        b"reinstall the plugin; the prompt was withheld.\n"
    )
    assert SECRET_PROMPT.encode() not in result.stdout + result.stderr


@pytest.mark.parametrize("client", PROMPT_CLIENTS)
def test_launcher_withholds_the_prompt_when_no_interpreter_exists(
    client: str, archive: Path, tmp_path: Path
) -> None:
    root = _plugin(tmp_path, archive)

    result = _run(
        client,
        _isolated(tmp_path, PATH="/nonexistent", CLAUDE_PLUGIN_ROOT=str(root)),
    )

    assert result.returncode == 0
    assert result.stdout == _error_output(client)
    assert (
        result.stderr == b"shim: no python3 found on PATH; the prompt was withheld.\n"
    )
    assert SECRET_PROMPT.encode() not in result.stdout + result.stderr


@pytest.mark.parametrize("client", TOOL_CLIENTS)
@pytest.mark.parametrize("bundled", (False, True), ids=("no-archive", "no-python"))
@pytest.mark.parametrize("call", (False, True), ids=("result", "call"))
def test_launcher_answers_a_tool_event_it_cannot_run_for(
    client: str, bundled: bool, call: bool, archive: Path, tmp_path: Path
) -> None:
    environment = _isolated(tmp_path, PATH="/nonexistent")
    if bundled:
        environment["CLAUDE_PLUGIN_ROOT"] = str(_plugin(tmp_path, archive))

    result = _run_tool_event(client, environment, call)

    _answered_uninspected(
        result,
        "no python3 found on PATH"
        if bundled
        else "no hook available. Install with 'uv tool install shim' or "
        "reinstall the plugin",
        call,
    )


def _failing(path: Path, code: int) -> Path:
    """An interpreter that resolves on PATH but cannot run.

    A pyenv shim whose pinned version is missing, the macOS xcode-select stub
    and a Python 2 rejecting `-I` all exit non-zero before reading stdin.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{path.name}: cannot run' >&2\nexit {code}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _unrunnable(tmp_path: Path, archive: Path) -> dict[str, str]:
    stubs = tmp_path / "stubs"
    _failing(stubs / "python3.14", 127)
    _failing(stubs / "python", 2)
    return _isolated(
        tmp_path, PATH=str(stubs), CLAUDE_PLUGIN_ROOT=str(_plugin(tmp_path, archive))
    )


@pytest.mark.parametrize("client", PROMPT_CLIENTS)
def test_launcher_withholds_the_prompt_when_no_interpreter_can_run(
    client: str, archive: Path, tmp_path: Path
) -> None:
    result = _run(client, _unrunnable(tmp_path, archive))

    assert result.returncode == 0
    assert result.stdout == _error_output(client)
    assert result.stderr == (
        b"python3.14: cannot run\npython: cannot run\n"
        b"shim: no python3 on PATH could run the bundled archive; "
        b"the prompt was withheld.\n"
    )


@pytest.mark.parametrize("client", TOOL_CLIENTS)
@pytest.mark.parametrize("call", (False, True), ids=("result", "call"))
def test_launcher_answers_a_tool_event_no_interpreter_could_run(
    client: str, call: bool, archive: Path, tmp_path: Path
) -> None:
    result = _run_tool_event(client, _unrunnable(tmp_path, archive), call)

    _answered_uninspected(
        result,
        "no python3 on PATH could run the bundled archive",
        call,
        stderr=b"python3.14: cannot run\npython: cannot run\n",
    )


@pytest.mark.parametrize("event", ("prompt", "tool"))
@pytest.mark.parametrize("route", ("package", "archive"))
def test_launcher_falls_through_an_interpreter_that_cannot_run(
    route: str, event: str, archive: Path, tmp_path: Path
) -> None:
    """The next candidate runs the hook; stdin was never read by the failure.

    `package` is a shim-hook whose venv lost shim_cli, or that needs a
    PYTHONPATH the isolated run drops; `archive` is a pyenv `python3.14` shim
    with no such version installed, ahead of a working `python3`.
    """
    working = tmp_path / "python"
    working.mkdir()
    (working / "python3").symlink_to(sys.executable)
    if route == "package":
        broken = _failing(tmp_path / "venv" / "bin" / "python3", 1)
        first = _package_hook(tmp_path / "bin", str(broken)).parent
    else:
        broken = _failing(tmp_path / "pyenv" / "shims" / "python3.14", 127)
        first = broken.parent
    config = tmp_path / "config.toml"
    config.write_text('[mode]\nuser-prompt = "enforce"\n', encoding="utf-8")
    environment = _isolated(
        tmp_path,
        PATH=f"{first}:{working}",
        CLAUDE_PLUGIN_ROOT=str(_plugin(tmp_path, archive)),
        SHIM_CONFIG=str(config),
    )

    if event == "prompt":
        result = _run("claude", environment, prompt="my key is AKIAIOSFODNN7EXAMPLE")
    else:
        result = _run_tool_event("claude", environment)

    assert result.returncode == 0
    assert result.stderr == f"{broken.name}: cannot run\n".encode()
    assert b"AKIAIOSFODNN7EXAMPLE" not in result.stdout
    document = json.loads(result.stdout)
    if event == "prompt":
        assert document["decision"] == "block"
        assert document["reason"].startswith("shim blocked this prompt: SECRET (1).\n")
    else:
        output = document["hookSpecificOutput"]["updatedToolOutput"]
        assert output["content"] == "AWS_ACCESS_KEY_ID=<SECRET_1>"


@pytest.mark.parametrize("client", CLIENTS)
def test_launcher_uses_the_bundled_archive_when_the_package_is_absent(
    client: str, archive: Path, tmp_path: Path
) -> None:
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "shim.pyz").write_bytes(archive.read_bytes())
    environment = {
        "PATH": _python_path(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(root),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "SHIM_CONFIG": str(tmp_path / "config.toml"),
    }

    result = _run(client, environment)

    assert result.returncode == 0
    assert result.stderr == b""
    document = json.loads(result.stdout)
    if client == "copilot":
        assert document["modifiedTransformedPrompt"] == "Contact <EMAIL_1>"
    else:
        assert "decision" not in document
        assert document["systemMessage"] == (
            "shim: found EMAIL (1) in your prompt. Not modified."
        )
    assert "alice@example.com" not in result.stdout.decode()


def _package_hook(directory: Path, interpreter: str) -> Path:
    """A `shim-hook` console script as uv, pipx and pip write it."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "shim-hook"
    script.write_text(
        f"#!{interpreter}\nimport sys\nfrom shim_cli.hook import main\n"
        "sys.exit(main())\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _broken_archive(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "shim.pyz").write_text("not a real archive", encoding="utf-8")
    return root


@pytest.mark.parametrize("client", CLIENTS)
def test_launcher_prefers_the_package_on_path(client: str, tmp_path: Path) -> None:
    interpreter = tmp_path / "venv" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    interpreter.chmod(0o755)
    script = _package_hook(tmp_path / "bin", str(interpreter))

    result = _run(
        client,
        _isolated(
            tmp_path,
            PATH=f"{script.parent}:/usr/bin:/bin",
            CLAUDE_PLUGIN_ROOT=str(_broken_archive(tmp_path)),
        ),
    )

    # Its own interpreter, with no PYTHON* variable reaching it; the prompt
    # marker is the launcher's and never reaches the hook.
    assert (result.returncode, result.stderr) == (0, b"")
    assert result.stdout.decode().splitlines() == ["-E", "-B", str(script), client]


@pytest.mark.parametrize("route", ("package", "archive"))
def test_a_project_module_cannot_stand_in_for_the_standard_library(
    route: str, archive: Path, tmp_path: Path
) -> None:
    """`PYTHONPATH=.` in a dev shell must not let a repository run in the hook.

    The planted modules exit silently, which is what a hook that passes
    everything through looks like to the client. `PYTHONINSPECT=1` alone turns
    every answer into a traceback and exit 1, which the client also ignores.
    """
    project = tmp_path / "project"
    project.mkdir()
    for name in ("token.py", "types.py", "json.py"):
        (project / name).write_text("import os\nos._exit(0)\n", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text('[mode]\nuser-prompt = "enforce"\n', encoding="utf-8")
    if route == "package":
        path = str(_package_hook(tmp_path / "bin", sys.executable).parent)
        root = _broken_archive(tmp_path)
    else:
        path = _python_path(tmp_path)
        root = _plugin(tmp_path, archive)
    environment = _isolated(
        tmp_path,
        PATH=path,
        CLAUDE_PLUGIN_ROOT=str(root),
        SHIM_CONFIG=str(config),
        PYTHONPATH=".",
        PYTHONINSPECT="1",
    )

    result = _run(
        "claude", environment, prompt="my key is AKIAIOSFODNN7EXAMPLE", cwd=project
    )

    assert (result.returncode, result.stderr) == (0, b"")
    document = json.loads(result.stdout)
    assert document["decision"] == "block"
    assert document["suppressOriginalPrompt"] is True
    assert document["reason"].startswith("shim blocked this prompt: SECRET (1).\n")
    assert b"AKIAIOSFODNN7EXAMPLE" not in result.stdout


@pytest.mark.parametrize(
    "shebang",
    ("{tmp}/gone/venv/bin/python3", "{tmp}/plain/python3"),
    ids=("removed-interpreter", "not-executable"),
)
def test_launcher_falls_back_to_the_archive_when_the_package_cannot_start(
    shebang: str, archive: Path, tmp_path: Path
) -> None:
    """A tool venv whose base Python was upgraded away leaves a dead shebang."""
    plain = tmp_path / "plain" / "python3"
    plain.parent.mkdir()
    plain.write_text("", encoding="utf-8")
    script = _package_hook(tmp_path / "bin", shebang.format(tmp=tmp_path))
    environment = _isolated(
        tmp_path,
        PATH=f"{script.parent}:{_python_path(tmp_path)}",
        CLAUDE_PLUGIN_ROOT=str(_plugin(tmp_path, archive)),
        SHIM_CONFIG=str(tmp_path / "config.toml"),
    )

    result = _run("claude", environment)

    assert (result.returncode, result.stderr) == (0, b"")
    assert json.loads(result.stdout)["systemMessage"] == (
        "shim: found EMAIL (1) in your prompt. Not modified."
    )


@pytest.mark.parametrize("client", CLIENTS)
def test_launcher_stays_silent_on_a_safe_prompt(
    client: str, archive: Path, tmp_path: Path
) -> None:
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "shim.pyz").write_bytes(archive.read_bytes())

    result = _run(
        client,
        {
            "PATH": _python_path(tmp_path),
            "CLAUDE_PLUGIN_ROOT": str(root),
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "SHIM_CONFIG": str(tmp_path / "config.toml"),
        },
        prompt="Explain merge sort.",
    )

    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


def test_launcher_is_executable_and_shell_free() -> None:
    assert os.access(LAUNCHER, os.X_OK)
    source = LAUNCHER.read_text(encoding="utf-8")
    assert source.startswith("#!/bin/sh\n")
    assert "eval" not in source


def test_archive_is_self_contained_and_within_budget(archive: Path) -> None:
    assert archive.stat().st_size < MAX_ARCHIVE_BYTES
    assert archive.read_bytes().startswith(b"#!/usr/bin/env python3\n")

    names = zipfile.ZipFile(archive).namelist()
    packaged = {name.split("/")[0] for name in names}

    assert packaged == {"__main__.py", "shim_cli", "phonenumbers", "tomli"}
    assert not any(name.startswith("shim_cli/cli/") for name in names)
    assert not any(name.endswith(".pyi") for name in names)
    assert not any(name.endswith((".so", ".pyd", ".dylib")) for name in names)
    for excluded in ("geodata", "carrierdata", "tzdata"):
        assert not any(f"phonenumbers/{excluded}/" in name for name in names)
    assert "shim_cli/guard/suffixes.py" in names
    assert "tomli/_parser.py" in names
    assert "shim_cli/events/html_text.py" in names


def _as_python_38(archive: Path, client: str, raw: bytes, tmp_path: Path):
    """Run the archive's `__main__` as an interpreter older than 3.9 sees it."""
    bootstrap = (
        "import runpy, sys; sys.version_info = (3, 8, 20, 'final', 0); "
        f"sys.argv = [{str(archive)!r}, {client!r}]; "
        f"runpy.run_path({str(archive)!r}, run_name='__main__')"
    )
    return subprocess.run(
        (sys.executable, "-I", "-B", "-c", bootstrap),
        input=raw,
        capture_output=True,
        check=False,
        env=_isolated(tmp_path),
        timeout=120,
    )


@pytest.mark.parametrize("client", PROMPT_CLIENTS)
@pytest.mark.parametrize(
    "raw",
    (
        None,
        b"",
        b"{not json",
        b'["UserPromptSubmit"]',
        b'{"hook_event_name":7}',
        *(json.dumps({"hook_event_name": name}).encode() for name in PROMPT_EVENTS),
    ),
    ids=(
        "prompt",
        "empty",
        "malformed",
        "not-an-object",
        "non-string-event",
        *PROMPT_EVENTS,
    ),
)
def test_archive_withholds_a_prompt_on_an_unsupported_interpreter(
    client: str, raw: bytes | None, archive: Path, tmp_path: Path
) -> None:
    result = _as_python_38(
        archive, client, _payload(client) if raw is None else raw, tmp_path
    )

    assert result.returncode == 0
    assert result.stdout == _error_output(client)
    assert result.stderr == (
        b"shim: needs Python 3.9 or newer; found 3.8.20. The prompt was withheld.\n"
    )
    assert SECRET_PROMPT.encode() not in result.stdout + result.stderr


@pytest.mark.parametrize("client", TOOL_CLIENTS)
@pytest.mark.parametrize("call", (False, True), ids=("result", "call"))
def test_archive_answers_a_tool_event_on_an_unsupported_interpreter(
    client: str, call: bool, archive: Path, tmp_path: Path
) -> None:
    result = _as_python_38(archive, client, TOOL_CALL if call else TOOL_EVENT, tmp_path)

    assert result.returncode == 0
    if call:
        assert result.stdout == _DENIED_CALL
        assert result.stderr == (
            b"shim: needs Python 3.9 or newer; found 3.8.20. "
            b"The tool call was not allowed.\n"
        )
    else:
        assert result.stderr == b""
        assert _uninspected(result.stdout) == (
            "shim: needs Python 3.9 or newer; found 3.8.20; "
            "this event was not inspected."
        )


def test_archive_main_parses_before_python_39(archive: Path, tmp_path: Path) -> None:
    source = zipfile.ZipFile(archive).read("__main__.py").decode()

    assert "MINIMUM = (3, 9)" in source
    assert 'f"' not in source, "must parse on interpreters without f-strings"
    ast.parse(source, feature_version=(3, 7))  # the oldest grammar ast still knows

    older = shutil.which("python3.8")
    if older is None:
        pytest.skip("python3.8 is not installed")
    result = subprocess.run(
        (older, "-I", "-B", str(archive), "claude"),
        input=_payload("claude"),
        capture_output=True,
        check=False,
        env=_isolated(tmp_path),
        timeout=120,
    )

    assert result.returncode == 0
    assert result.stdout == _error_output("claude")
    assert result.stderr.startswith(b"shim: needs Python 3.9 or newer; found 3.8.")


# Every fixture leaves the ASCII fast path in `normalize()`. An ASCII-only set
# passed on 3.9 for a year while `zip(strict=True)` made non-ASCII text raise.
FIXTURES = {
    "prompt": (
        '{"hook_event_name":"UserPromptSubmit",'
        '"prompt":"Hesap \u0131\u015fl\u011fi: TR330006100519786457841326"}'
    ).encode("utf-8"),
    "tool": (
        '{"hook_event_name":"PostToolUse","tool_name":"Read",'
        '"tool_response":{"content":"kanit \u2014 AKIAIOSFODNN7EXAMPLE"}}'
    ).encode("utf-8"),
    "tool-json": (
        '{"hook_event_name":"PostToolUse","tool_name":"Read",'
        '"tool_response":{"content":"{\\"m\u00fc\u015fteri\\":\\"Test User\\",'
        '\\"created\\":1757496600,\\"telefon\\":\\"0532 123 45 67\\"}"}}'
    ).encode("utf-8"),
    "stop": (
        '{"hook_event_name":"Stop","session_id":"s1",'
        '"last_assistant_message":"bitti \u2014 alice@example.com"}'
    ).encode("utf-8"),
    "tool-html": json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__fetch__fetch",
            "tool_input": {"url": "http://127.0.0.1/page.html"},
            "tool_response": {
                "text": "<!doctype html><html><head><title>Durum \u2014 sayfa"
                "</title></head><body><h2>\u0130kinci ba\u015fl\u0131k</h2>"
                "<p>Yaz: alice@example.com &amp; <a href='/x'>ba\u011flant\u0131</a>"
                "</p><table><tr><td>a</td><td>1</td></tr></table>"
                + "<div class='row'><span>sat\u0131r</span></div>" * 80
                + "</body></html>"
            },
        }
    ).encode("utf-8"),
}


def test_the_archive_answers_identically_on_39_and_the_current_interpreter(
    archive: Path, tmp_path: Path
) -> None:
    """The zero-install path targets a stock macOS, whose python3 is 3.9."""
    old = shutil.which("python3.9")
    if old is None:
        pytest.skip("python3.9 is not installed")
    config = tmp_path / "c.toml"
    config.write_text(
        'enabled_entities = ["EMAIL", "IBAN", "PHONE", "SECRET"]\n'
        'diet = ["json", "html"]\n\n'
        '[mode]\nuser-prompt = "enforce"\n',
        encoding="utf-8",
    )

    def answer(interpreter: str, raw: bytes) -> tuple[int, bytes, bytes]:
        home = tmp_path / Path(interpreter).name
        home.mkdir(exist_ok=True)
        result = subprocess.run(
            (interpreter, str(archive), "claude"),
            input=raw,
            capture_output=True,
            check=False,
            # Each interpreter needs its own spool, or the second run
            # summarises the first run's records too.
            env=os.environ
            | {
                "TMPDIR": str(home),
                "SHIM_CONFIG": str(config),
                "XDG_STATE_HOME": str(home / "state"),
            },
            timeout=120,
        )
        scrub = re.compile(rb"shim-redacted-[^\"]+")
        timing = re.compile(rb"\d+ ms")
        return (
            result.returncode,
            timing.sub(
                b"N ms",
                scrub.sub(b"X", result.stdout.replace(str(home).encode(), b"<tmp>")),
            ),
            result.stderr,
        )

    answers = {name: answer(sys.executable, raw) for name, raw in FIXTURES.items()}
    for name, raw in FIXTURES.items():
        assert answer(old, raw) == answers[name], name
    output = json.loads(answers["tool-json"][1])["hookSpecificOutput"]
    document = json.loads(output["updatedToolOutput"]["content"])
    assert (document["created"], document["telefon"]) == (1757496600, "<PHONE_1>")
    page = json.loads(answers["tool-html"][1])["hookSpecificOutput"]
    assert page["updatedToolOutput"]["text"].split("\n")[:4] == [
        "[shim: converted from HTML to text; markup removed. Save the page to a "
        "file and Read it to see the markup.]",
        "Durum \u2014 sayfa",
        "## \u0130kinci ba\u015fl\u0131k",
        "Yaz: <EMAIL_1> & ba\u011flant\u0131 (/x)",
    ]


def _archive_members(path: Path) -> dict[str, bytes]:
    prefix, marker, _ = path.read_bytes().partition(b"PK\x03\x04")
    assert marker and prefix == b"#!/usr/bin/env python3\n"
    with zipfile.ZipFile(path) as packaged:
        infos = packaged.infolist()
        names = [info.filename for info in infos]
        assert len(names) == len(set(names))
        assert packaged.comment == b""
        for info in infos:
            assert info.date_time == (2000, 1, 1, 0, 0, 0)
            mode = 0o755 if info.is_dir() else 0o644
            assert (info.external_attr >> 16) & 0o777 == mode
        return {name: packaged.read(name) for name in names}


def test_archive_version_reads_the_archive(tmp_path: Path) -> None:
    from shim_cli.cli.resolution import archive_version

    bundle = tmp_path / "shim_cli.pyz"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("shim_cli/__init__.py", '__version__ = "9.9.9"\n')
    assert archive_version(bundle) == "9.9.9"

    empty = tmp_path / "empty.pyz"
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("__main__.py", "")
    assert archive_version(empty) is None


def test_committed_archive_matches_a_fresh_build(archive: Path) -> None:
    assert COMMITTED.is_file(), "build plugins/shim-cli/bin/shim.pyz"
    assert os.access(COMMITTED, os.X_OK)
    assert COMMITTED.stat().st_size < MAX_ARCHIVE_BYTES
    assert _archive_members(COMMITTED) == _archive_members(archive), (
        "rebuild plugins/shim-cli/bin/shim.pyz"
    )


def test_archive_build_is_reproducible(archive: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.pyz"
    subprocess.run(
        (sys.executable, str(BUILDER), "--output", str(other)),
        check=True,
        env=os.environ | {"TZ": "America/Los_Angeles"},
        preexec_fn=partial(os.umask, 0o077),
        timeout=300,
    )
    assert archive.read_bytes() == other.read_bytes()


def test_archive_contains_every_module_the_hook_path_imports(archive: Path) -> None:
    probe = (
        "import json, sys\n"
        "from shim_cli import hook\n"
        "for client in ('claude', 'codex', 'copilot'):\n"
        "    payload = json.dumps({'hook_event_name': 'UserPromptSubmit',"
        " 'prompt': 'Contact alice@example.com'}).encode()\n"
        "    hook._output(payload, client)\n"
        "for name in ('PreToolUse', 'PostToolUse'):\n"
        "    payload = json.dumps({'hook_event_name': name, 'tool_name': 'Read',"
        " 'tool_input': {'file_path': 'x'},"
        " 'tool_response': {'text': 'Contact alice@example.com'}}).encode()\n"
        "    hook._output(payload, 'claude')\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('shim_cli'))))"
    )
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", probe),
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr.decode()
    imported = json.loads(result.stdout)

    packaged = {
        name[: -len(".py")].replace("/", ".")
        for name in zipfile.ZipFile(archive).namelist()
        if name.startswith("shim_cli/") and name.endswith(".py")
    }
    packaged |= {
        name.rsplit(".", 1)[0] for name in list(packaged) if name.endswith(".__init__")
    }
    packaged = {name.replace(".__init__", "") for name in packaged}

    missing = set(imported) - packaged
    assert not missing, f"add these to build_zipapp.INCLUDED: {sorted(missing)}"


def test_launcher_finds_an_interpreter_named_python3_14(
    archive: Path, tmp_path: Path
) -> None:
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "shim.pyz").write_bytes(archive.read_bytes())
    interpreters = tmp_path / "interpreters"
    interpreters.mkdir()
    (interpreters / "python3.14").symlink_to(sys.executable)
    environment = {
        "PATH": str(interpreters),
        "CLAUDE_PLUGIN_ROOT": str(root),
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "SHIM_CONFIG": str(tmp_path / "config.toml"),
    }

    result = _run("claude", environment)

    assert (result.returncode, result.stderr) == (0, b"")
    assert json.loads(result.stdout)["systemMessage"] == (
        "shim: found EMAIL (1) in your prompt. Not modified."
    )


WINDOWS_NOTICE = (
    b"shim: shim-cli does not support Windows yet; nothing was inspected.\n"
)


def _uname(tmp_path: Path, kernel: str) -> str:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    stub = stubs / "uname"
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' '{kernel}'\n", encoding="utf-8")
    stub.chmod(0o755)
    return str(stubs)


@pytest.mark.parametrize(
    "kernel", ("MINGW64_NT-10.0-19045", "MSYS_NT-10.0", "CYGWIN_NT-10.0")
)
def test_launcher_stands_down_under_git_bash(kernel: str, tmp_path: Path) -> None:
    result = _run("claude", {"PATH": _uname(tmp_path, kernel)})

    assert (result.returncode, result.stdout, result.stderr) == (0, b"", WINDOWS_NOTICE)


def test_launcher_stands_down_when_os_says_windows(tmp_path: Path) -> None:
    environment = {"PATH": _uname(tmp_path, "Linux"), "OS": "Windows_NT"}

    result = _run("claude", environment)

    assert (result.returncode, result.stdout, result.stderr) == (0, b"", WINDOWS_NOTICE)


def test_launcher_under_wsl_is_not_windows(tmp_path: Path) -> None:
    result = _run("claude", {"PATH": _uname(tmp_path, "Linux")})

    assert result.returncode == 0
    assert WINDOWS_NOTICE not in result.stderr
    assert b"no hook available" in result.stderr


def test_the_archive_stands_down_on_windows(archive: Path, tmp_path: Path) -> None:
    bootstrap = (
        "import runpy, shutil, signal, sys, tempfile; sys.platform = 'win32'; "
        "sys.modules['fcntl'] = None; "
        "del signal.SIGALRM, signal.setitimer, signal.ITIMER_REAL; "
        f"sys.argv = [{str(archive)!r}, 'claude']; "
        f"runpy.run_path({str(archive)!r}, run_name='__main__')"
    )

    result = subprocess.run(
        (sys.executable, "-I", "-c", bootstrap),
        input=_payload("claude"),
        capture_output=True,
        check=False,
        timeout=120,
        env={"TMPDIR": str(tmp_path), "HOME": str(tmp_path)},
    )

    assert (result.returncode, result.stdout, result.stderr) == (0, b"", WINDOWS_NOTICE)
