from __future__ import annotations

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

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "shim-cli"
LAUNCHER = PLUGIN / "hooks" / "run-shim"
BUILDER = ROOT / "scripts" / "build_zipapp.py"
COMMITTED = PLUGIN / "bin" / "shim.pyz"
MAX_ARCHIVE_BYTES = 500_000
CLIENTS = ("claude", "codex", "copilot")
SECRET_PROMPT = "Contact alice@example.com"


def _payload(client: str, prompt: str = SECRET_PROMPT) -> bytes:
    if client == "copilot":
        return json.dumps({"prompt": prompt, "transformedPrompt": prompt}).encode()
    return json.dumps(
        {"hook_event_name": "UserPromptSubmit", "prompt": prompt}
    ).encode()


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


def _run(client: str, environment: dict[str, str], prompt: str = SECRET_PROMPT):
    return subprocess.run(
        (str(LAUNCHER), client),
        input=_payload(client, prompt),
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


@pytest.mark.parametrize("client", CLIENTS)
def test_launcher_allows_the_prompt_when_nothing_is_runnable(client: str) -> None:
    result = _run(client, {"PATH": "/nonexistent"})

    assert result.returncode == 0
    assert result.stdout == b"", "an unrunnable guard must not block the prompt"
    assert result.stderr, "the user must be told once why nothing was inspected"
    assert result.stderr.count(b"\n") == 1, "exactly one line of explanation"
    assert b"shim" in result.stderr.lower()
    assert SECRET_PROMPT.encode() not in result.stdout + result.stderr


@pytest.mark.parametrize("client", CLIENTS)
def test_launcher_allows_the_prompt_when_no_interpreter_exists(
    client: str, archive: Path, tmp_path: Path
) -> None:
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "shim.pyz").write_bytes(archive.read_bytes())

    result = _run(client, {"PATH": "/nonexistent", "CLAUDE_PLUGIN_ROOT": str(root)})

    assert result.returncode == 0
    assert result.stdout == b""
    assert b"python3" in result.stderr
    assert SECRET_PROMPT.encode() not in result.stdout + result.stderr


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
        "SHIM_GUARD_CONFIG": str(tmp_path / "config.toml"),
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


@pytest.mark.parametrize("client", CLIENTS)
def test_launcher_prefers_the_package_on_path(client: str, tmp_path: Path) -> None:
    marker = tmp_path / "shim-guard-hook"
    marker.write_text("#!/bin/sh\nprintf '%s' \"PATH-HOOK:$1\"\n", encoding="utf-8")
    marker.chmod(0o755)
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "shim.pyz").write_text("not a real archive", encoding="utf-8")

    result = _run(
        client,
        {"PATH": f"{tmp_path}:/usr/bin:/bin", "CLAUDE_PLUGIN_ROOT": str(root)},
    )

    assert result.stdout == f"PATH-HOOK:{client}".encode()


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
            "SHIM_GUARD_CONFIG": str(tmp_path / "config.toml"),
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


def test_archive_refuses_an_unsupported_interpreter_without_blocking(
    archive: Path,
) -> None:
    source = zipfile.ZipFile(archive).read("__main__.py").decode()

    assert "MINIMUM = (3, 9)" in source
    assert "sys.exit(0)" in source
    assert 'f"' not in source, "must parse on interpreters without f-strings"

    older = shutil.which("python3.8")
    if older is None:
        pytest.skip("python3.8 is not installed")
    result = subprocess.run(
        (older, str(archive), "claude"),
        input=_payload("claude"),
        capture_output=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr.startswith(b"shim: needs Python 3.9 or newer; found 3.8.")


FIXTURES = {
    "prompt": b'{"hook_event_name":"UserPromptSubmit","prompt":"Contact alice@example.com"}',
    "tool": (
        b'{"hook_event_name":"PostToolUse","tool_name":"Read",'
        b'"tool_response":{"content":"key=alice@example.com"}}'
    ),
    "stop": b'{"hook_event_name":"Stop","session_id":"s1"}',
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
        'enabled_entities = ["EMAIL"]\n\n[mode]\nuser-prompt = "enforce"\n',
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
            env=os.environ | {"TMPDIR": str(home), "SHIM_CONFIG": str(config)},
            timeout=120,
        )
        scrub = re.compile(rb"shim-redacted-[^\"]+")
        return (
            result.returncode,
            scrub.sub(b"X", result.stdout.replace(str(home).encode(), b"<tmp>")),
            result.stderr,
        )

    for name, raw in FIXTURES.items():
        assert answer(old, raw) == answer(sys.executable, raw), name


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


def test_archive_version_reads_the_new_layout_and_the_old_one(tmp_path: Path) -> None:
    from shim_cli.cli.resolution import archive_version

    for package, expected in (("shim_cli", "9.9.9"), ("shim_guard", "0.2.0")):
        bundle = tmp_path / f"{package}.pyz"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr(f"{package}/__init__.py", f'__version__ = "{expected}"\n')
        assert archive_version(bundle) == expected

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
