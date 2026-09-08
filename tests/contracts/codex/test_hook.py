from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMMAND = (sys.executable, "-I", "-B", "-m", "shim_cli.hook")
GENERIC_BLOCK = (
    b'{"decision":"block","reason":"shim could not inspect this prompt, '
    b'so it was withheld. Run `shim doctor codex` for the reason."}'
)
COPY_INSTRUCTION = "Copy and paste this as your next prompt:"
READ_INSTRUCTION = "Read this file and use its contents as my prompt: "

ENFORCE_PROMPT = (
    'enabled_entities = ["EMAIL", "PHONE", "CREDIT_CARD", "IBAN", "IP_ADDRESS", '
    '"MAC_ADDRESS", "US_SSN", "TR_NATIONAL_ID", "TR_VKN", "SECRET", "DB_URI"]\n'
    "\n[mode]\n"
    'user-prompt = "enforce"\n'
)


def _enforcing(tmp_path: Path, **extra: str) -> dict:
    target = tmp_path / "enforce.toml"
    target.write_text(ENFORCE_PROMPT, encoding="utf-8")
    environment = os.environ.copy()
    environment["SHIM_GUARD_CONFIG"] = str(target)
    environment["TMPDIR"] = str(tmp_path)
    environment.update(extra)
    return environment


def _payload(prompt: str, **changes: object) -> bytes:
    event: dict[str, object] = {
        "session_id": "thr_test",
        "transcript_path": None,
        "cwd": "/workspace",
        "hook_event_name": "UserPromptSubmit",
        "model": "gpt-5",
        "turn_id": "turn_test",
        "permission_mode": "default",
        "prompt": prompt,
    }
    event.update(changes)
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()


def _redaction_files(root):
    return [
        item
        for item in root.rglob("*")
        if item.is_file() and item.suffix not in (".jsonl", ".mark")
    ]


def _run(
    raw: bytes,
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        COMMAND,
        input=raw,
        capture_output=True,
        cwd=cwd,
        env=env,
        check=False,
        timeout=60,
    )


def _assert_output(result: subprocess.CompletedProcess[bytes], expected: bytes) -> None:
    assert result.returncode == 0
    assert result.stdout == expected
    assert result.stderr == b""


def _suggestion_path(result: subprocess.CompletedProcess[bytes], summary: str) -> Path:
    assert result.returncode == 0
    assert result.stderr == b""
    document = json.loads(result.stdout)
    assert document["decision"] == "block"
    reason = document["reason"]
    lines = reason.splitlines()
    assert len(lines) == 3
    assert lines[:2] == [summary, COPY_INSTRUCTION]
    assert lines[2].startswith(READ_INSTRUCTION)
    path = Path(lines[2].removeprefix(READ_INSTRUCTION))
    assert path.is_absolute()
    assert path.is_file()
    return path


def test_safe_prompt_is_byte_for_byte_silent() -> None:
    _assert_output(_run(_payload("Explain merge sort. Merhaba İstanbul 🌍")), b"")


def test_finding_writes_a_secure_redacted_prompt(tmp_path: Path) -> None:
    environment = _enforcing(tmp_path)
    result = _run(_payload("Contact alice@example.com"), env=environment)
    path = _suggestion_path(result, "shim blocked this prompt: EMAIL (1).")

    assert b"alice@example.com" not in result.stdout
    assert path.parent == tmp_path
    assert path.read_text() == "Contact <EMAIL_1>"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_hook_honors_entity_settings_and_rejects_invalid_settings(
    tmp_path: Path,
) -> None:
    from shim_cli.config import render_entities

    target = tmp_path / "settings" / "config.toml"
    target.parent.mkdir()
    target.write_bytes(
        render_entities(("PHONE",)) + b'\n[mode]\nuser-prompt = "enforce"\n'
    )
    environment = _enforcing(tmp_path)
    environment["SHIM_GUARD_CONFIG"] = str(target)

    _assert_output(_run(_payload("Contact alice@example.com"), env=environment), b"")
    phone = _run(_payload("Call +90 532 123 45 67"), env=environment)
    path = _suggestion_path(phone, "shim blocked this prompt: PHONE (1).")
    assert path.read_text() == "Call <PHONE_1>"

    target.write_bytes(b"invalid")
    _assert_output(_run(_payload("Explain merge sort"), env=environment), GENERIC_BLOCK)


def test_block_reason_excludes_terminal_controls_from_the_prompt(
    tmp_path: Path,
) -> None:
    controls = "\x1b]0;owned\x07\u009b31m"
    environment = _enforcing(tmp_path)
    result = _run(_payload(f"Contact alice@example.com{controls}"), env=environment)
    reason = json.loads(result.stdout)["reason"]

    assert result.returncode == 0
    assert result.stderr == b""
    assert all(character not in reason for character in "\x1b\x07\u009b")


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{",
        b"\xff",
        b"[]",
        b'{"hook_event_name":"UserPromptSubmit","prompt":["hello"]}',
        b'{"prompt":"hello"}',
        b'{"hook_event_name":"UserPromptSubmit"}',
        b'{"hook_event_name":"UserPromptSubmit","prompt":7}',
        b'{"hook_event_name":"UserPromptSubmit","prompt":"a"} {}',
        b'{"hook_event_name":"UserPromptSubmit","prompt":"a","prompt":"b"}',
        b'{"hook_event_name":"UserPromptSubmit","hook_event_name":"PostToolUse",'
        b'"prompt":"hello"}',
        b'{"hook_event_name":"PostToolUse","hook_event_name":"UserPromptSubmit",'
        b'"prompt":"hello"}',
        rb'{"hook_event_name":"PostToolUse","hook_\u0065vent_name":'
        rb'"UserPromptSubmit","prompt":"hello"}',
        rb'{"hook_event_name":"PostToolUse","hook_event_name":'
        rb'"UserPrompt\u0053ubmit","prompt":"hello"}',
        b'{"prompt":"hook_event_name":"PostToolUse"',
    ],
)
def test_hostile_input_fails_closed(raw: bytes) -> None:
    _assert_output(_run(raw), GENERIC_BLOCK)


def test_nested_prompt_event_does_not_reclassify_malformed_tool_event() -> None:
    raw = (
        b'{"tool_input":{"hook_event_name":"UserPromptSubmit"},'
        b'"hook_event_name":"PostToolUse",'
    )
    _assert_output(_run(raw), b"")


@pytest.mark.parametrize(
    "raw",
    [
        rb'{"hook_\u0065vent_name":"PostToolUse","x":1,"x":2}',
        b'{"hook_event_name":"PostToolUse","value":"\xff"}',
    ],
)
def test_decodable_tool_event_fails_open_after_strict_parse_error(raw: bytes) -> None:
    _assert_output(_run(raw), b"")


@pytest.mark.parametrize(
    "raw",
    [
        b"x" * 1_000_001,
        _payload("x" * 1_000_000),
    ],
    ids=("oversized-bytes", "oversized-text"),
)
def test_oversized_input_fails_closed(raw: bytes) -> None:
    assert len(raw) > 1_000_000
    _assert_output(_run(raw), GENERIC_BLOCK)


def test_block_output_is_bounded_and_contains_no_raw_values(tmp_path: Path) -> None:
    raw_values = [f"person{index}@example.com" for index in range(50)]
    environment = _enforcing(tmp_path)
    result = _run(_payload(" ".join(raw_values)), env=environment)
    path = _suggestion_path(result, "shim blocked this prompt: EMAIL (50).")

    assert len(result.stdout) <= 4_096
    assert all(value.encode() not in result.stdout for value in raw_values)
    assert all(value not in path.read_text() for value in raw_values)


def test_environment_secret_and_dependency_noise_are_suppressed() -> None:
    secret = "SHIM_TEST_ENV_SECRET_4f8d1"
    code = r"""
import os
import sys
import types
import warnings
import shim_cli.guard as guard
from shim_cli import hook as runner
from shim_cli.clients import user_prompt_hook

clients = types.ModuleType("shim_cli.clients")
clients.__path__ = []
codex = types.ModuleType("shim_cli.clients.codex")
codex.__path__ = []
adapter = types.ModuleType("shim_cli.clients.codex.hook")

def noisy(label):
    secret = os.environ["SHIM_TEST_SECRET"]
    print(label, secret)
    print(label, secret, file=sys.stderr)
    os.write(1, (label + " fd1 " + secret).encode())
    os.write(2, (label + " fd2 " + secret).encode())
    warnings.warn(label + " warning " + secret)

def parse_input(raw):
    noisy("parse")
    return "safe"

def evaluate(prompt, enabled_entities):
    noisy("evaluate")
    return types.SimpleNamespace(blocked=False)

def block_output(decision, suggestion_path=None):
    noisy("serialize")
    return b""

adapter.parse_input = parse_input
adapter.block_output = block_output
adapter.warn_output = lambda decision: b""
adapter.error_output = lambda: b"unreachable"
guard.evaluate = evaluate
sys.modules.update({
    "shim_cli.clients": clients,
    "shim_cli.clients.user_prompt_hook": user_prompt_hook,
    "shim_cli.clients.codex": codex,
    "shim_cli.clients.codex.hook": adapter,
})
runner.main()
"""
    env = os.environ.copy()
    env["SHIM_TEST_SECRET"] = secret
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", code),
        input=b"{}",
        capture_output=True,
        cwd=ROOT,
        env=env,
        check=False,
        timeout=30,
    )

    _assert_output(result, b"")
    assert secret.encode() not in result.stdout + result.stderr


@pytest.mark.parametrize("stage", ["detector", "serialization"])
def test_dependency_errors_use_the_same_generic_block(
    stage: str, tmp_path: Path
) -> None:
    code = r"""
import os
import sys
import types
from shim_cli import hook as runner
from shim_cli.clients import user_prompt_hook

clients = types.ModuleType("shim_cli.clients")
clients.__path__ = []
codex = types.ModuleType("shim_cli.clients.codex")
codex.__path__ = []
adapter = types.ModuleType("shim_cli.clients.codex.hook")
guard = types.ModuleType("shim_cli.guard")

adapter.parse_input = lambda raw: "safe"
adapter.warn_output = lambda decision: b""
adapter.error_output = lambda: runner._ERROR_OUTPUT

def evaluate(prompt, enabled_entities):
    if os.environ["SHIM_TEST_STAGE"] == "detector":
        raise RuntimeError(os.environ["SHIM_TEST_SECRET"])
    return types.SimpleNamespace(blocked=True, redacted_text="redacted")

def block_output(decision, suggestion_path=None):
    if os.environ["SHIM_TEST_STAGE"] == "serialization":
        raise RuntimeError(os.environ["SHIM_TEST_SECRET"])
    return b""

adapter.block_output = block_output
guard.evaluate = evaluate
sys.modules.update({
    "shim_cli.clients": clients,
    "shim_cli.clients.user_prompt_hook": user_prompt_hook,
    "shim_cli.clients.codex": codex,
    "shim_cli.clients.codex.hook": adapter,
    "shim_cli.guard": guard,
})
runner.main()
"""
    env = os.environ.copy()
    env["SHIM_TEST_STAGE"] = stage
    env["SHIM_TEST_SECRET"] = "raw-value-must-not-leak"
    settings = tmp_path.parent / f"{tmp_path.name}-enforce.toml"
    settings.write_text(ENFORCE_PROMPT, encoding="utf-8")
    env["SHIM_GUARD_CONFIG"] = str(settings)
    env["TMPDIR"] = str(tmp_path)
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", code),
        input=b"{}",
        capture_output=True,
        cwd=ROOT,
        env=env,
        check=False,
        timeout=30,
    )

    _assert_output(result, GENERIC_BLOCK)
    assert b"raw-value-must-not-leak" not in result.stdout + result.stderr
    assert not _redaction_files(tmp_path)


def test_adapter_import_error_uses_the_same_generic_block() -> None:
    code = r"""
import builtins
import os
from shim_cli import hook as runner

real_import = builtins.__import__

def fail_adapter_import(name, *args, **kwargs):
    if name == "shim_cli.clients.codex.hook":
        raise ImportError(os.environ["SHIM_TEST_SECRET"])
    return real_import(name, *args, **kwargs)

builtins.__import__ = fail_adapter_import
runner.main()
"""
    env = os.environ.copy()
    env["SHIM_TEST_SECRET"] = "import-error-must-not-leak"
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", code),
        input=b"{}",
        capture_output=True,
        cwd=ROOT,
        env=env,
        check=False,
        timeout=30,
    )

    _assert_output(result, GENERIC_BLOCK)
    assert b"import-error-must-not-leak" not in result.stdout + result.stderr


def test_hook_processing_deadline_uses_the_generic_block() -> None:
    code = r"""
import sys
import time
import types
from shim_cli import hook as runner
from shim_cli.clients import user_prompt_hook

clients = types.ModuleType("shim_cli.clients")
clients.__path__ = []
codex = types.ModuleType("shim_cli.clients.codex")
codex.__path__ = []
adapter = types.ModuleType("shim_cli.clients.codex.hook")
guard = types.ModuleType("shim_cli.guard")
adapter.parse_input = lambda raw: "safe"
adapter.block_output = lambda decision, suggestion_path=None: b""
adapter.error_output = lambda: runner._ERROR_OUTPUT
guard.evaluate = lambda prompt, enabled_entities: time.sleep(1)
sys.modules.update({
    "shim_cli.clients": clients,
    "shim_cli.clients.user_prompt_hook": user_prompt_hook,
    "shim_cli.clients.codex": codex,
    "shim_cli.clients.codex.hook": adapter,
    "shim_cli.guard": guard,
})
runner.HOOK_DEADLINE_SECONDS = 0.05
runner.main()
"""
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", code),
        input=b"{}",
        capture_output=True,
        cwd=ROOT,
        check=False,
        timeout=5,
    )

    _assert_output(result, GENERIC_BLOCK)


def test_hook_deadline_includes_waiting_for_stdin_eof() -> None:
    code = (
        "from shim_cli import hook as runner; "
        "runner.HOOK_DEADLINE_SECONDS = 0.05; runner.main()"
    )
    with subprocess.Popen(
        (sys.executable, "-I", "-B", "-c", code),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
    ) as process:
        assert process.stdin is not None
        process.stdin.write(b"{")
        process.stdin.flush()
        process.wait(timeout=5)
        process.stdin.close()
        assert process.stdout is not None
        assert process.stderr is not None
        assert process.returncode == 0
        assert process.stdout.read() == GENERIC_BLOCK
        assert process.stderr.read() == b""


def test_isolated_mode_ignores_a_hostile_working_directory(tmp_path: Path) -> None:
    marker = tmp_path / "imported"
    package = tmp_path / "shim_cli"
    package.mkdir()
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
    )

    result = _run(_payload("Explain merge sort."), cwd=tmp_path)

    _assert_output(result, b"")
    assert not marker.exists()


def test_hook_persists_only_the_redacted_prompt_in_os_temp(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    temporary = tmp_path / "tmp"
    work = tmp_path / "work"
    for directory in (home, cache, temporary, work):
        directory.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(cache),
            "TMPDIR": str(temporary),
        }
    )
    config = tmp_path / "enforce.toml"
    config.write_text(ENFORCE_PROMPT, encoding="utf-8")
    env["SHIM_GUARD_CONFIG"] = str(config)
    prompt = "Contact persistence-canary@example.com"
    before = {item for item in tmp_path.rglob("*") if item.is_file()}

    result = _run(_payload(prompt), cwd=work, env=env)
    path = _suggestion_path(result, "shim blocked this prompt: EMAIL (1).")
    written = {item for item in tmp_path.rglob("*") if item.is_file()} - before
    spools = {item for item in written if item.suffix in (".jsonl", ".mark")}

    assert prompt.encode() not in result.stdout
    assert written == {path} | spools
    assert path.read_text() == "Contact <EMAIL_1>"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert spools, "the decision was not recorded"
    for spool in spools:
        assert stat.S_IMODE(spool.stat().st_mode) == 0o600
        content = spool.read_text()
        assert "persistence-canary" not in content
        assert prompt not in content
    entries = [
        json.loads(line)
        for spool in spools
        if spool.suffix == ".jsonl"
        for line in spool.read_text().splitlines()
    ]
    assert [entry["entities"] for entry in entries] == [{"EMAIL": 1}]
    assert [entry["action"] for entry in entries] == ["deny"]


def test_hook_does_not_attempt_network_access(tmp_path: Path) -> None:
    code = r"""
import socket

def no_network(*args, **kwargs):
    raise AssertionError("network access attempted")

socket.socket.connect = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network

from shim_cli import hook
hook.main()
"""
    environment = _enforcing(tmp_path)
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", code),
        input=_payload("Contact alice@example.com"),
        capture_output=True,
        cwd=ROOT,
        env=environment,
        check=False,
        timeout=60,
    )

    path = _suggestion_path(result, "shim blocked this prompt: EMAIL (1).")
    assert path.read_text() == "Contact <EMAIL_1>"
