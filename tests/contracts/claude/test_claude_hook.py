from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COMMAND = (sys.executable, "-I", "-B", "-m", "shim_cli.hook", "claude")
GENERIC_BLOCK = (
    b'{"decision":"block","reason":"shim could not inspect this prompt, '
    b'so it was withheld. Run `shim doctor claude` for the reason.",'
    b'"suppressOriginalPrompt":true}'
)
READ_INSTRUCTION = "Read this file and use its contents as my prompt: "


def _redaction_files(root):
    return [
        item
        for item in root.rglob("*")
        if item.is_file() and item.suffix not in (".jsonl", ".mark")
    ]


def _run(
    raw: bytes, tmp_path: Path, env_extra: dict | None = None
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["TMPDIR"] = str(tmp_path)
    environment.update(env_extra or {})
    return subprocess.run(
        COMMAND,
        input=raw,
        capture_output=True,
        cwd=ROOT,
        env=environment,
        check=False,
        timeout=60,
    )


def _payload(prompt: str) -> bytes:
    return json.dumps(
        {
            "session_id": "session",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/workspace",
            "permission_mode": "default",
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        },
        separators=(",", ":"),
    ).encode()


def test_claude_code_runner_allows_safe_prompts_silently(tmp_path: Path) -> None:
    result = _run(_payload("Explain merge sort."), tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


ENFORCE_PROMPT = (
    'enabled_entities = ["EMAIL", "PHONE", "CREDIT_CARD", "IBAN", "IP_ADDRESS", '
    '"MAC_ADDRESS", "US_SSN", "TR_NATIONAL_ID", "TR_VKN", "SECRET", "DB_URI"]\n'
    "\n[mode]\n"
    'user-prompt = "enforce"\n'
)


def test_claude_code_runner_reports_a_finding_and_lets_the_prompt_through(
    tmp_path: Path,
) -> None:
    result = _run(_payload("Contact alice@example.com"), tmp_path)
    document = json.loads(result.stdout)

    assert result.returncode == 0
    assert result.stderr == b""
    assert "decision" not in document
    assert document["systemMessage"] == (
        "shim: found EMAIL (1) in your prompt. Not modified."
    )
    assert b"alice@example.com" not in result.stdout
    assert not _redaction_files(tmp_path), "warning must not write a redaction file"


def test_claude_code_runner_blocks_with_a_private_redaction(tmp_path: Path) -> None:
    settings = tmp_path / "enforce.toml"
    settings.write_text(ENFORCE_PROMPT, encoding="utf-8")
    result = _run(
        _payload("Contact alice@example.com"),
        tmp_path,
        env_extra={"SHIM_GUARD_CONFIG": str(settings)},
    )
    document = json.loads(result.stdout)
    path = Path(document["reason"].split(READ_INSTRUCTION, 1)[1])

    assert result.returncode == 0
    assert result.stderr == b""
    assert document["decision"] == "block"
    assert document["suppressOriginalPrompt"] is True
    assert b"alice@example.com" not in result.stdout
    assert path.read_text() == "Contact <EMAIL_1>"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_claude_code_runner_fails_closed_on_invalid_input(tmp_path: Path) -> None:
    result = _run(b'{"hook_event_name":"UserPromptSubmit"}', tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (0, GENERIC_BLOCK, b"")


def test_claude_code_runner_does_not_block_a_truncated_tool_event(
    tmp_path: Path,
) -> None:
    result = _run(b'{"hook_event_name":"PostToolUse",', tmp_path)
    document = json.loads(result.stdout)

    assert result.returncode == 0
    assert result.stderr == b""
    assert "decision" not in document
    assert "could not be inspected" in document["systemMessage"]
