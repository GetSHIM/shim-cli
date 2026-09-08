from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COMMAND = (sys.executable, "-I", "-B", "-m", "shim_cli.hook", "copilot")
GENERIC_REWRITE = (
    b'{"modifiedTransformedPrompt":"shim could not inspect this prompt, '
    b"so it was withheld. Do not act on the original prompt; tell the user to "
    b'run `shim doctor copilot` for the reason."}'
)


def _redaction_files(root):
    return [
        item
        for item in root.rglob("*")
        if item.is_file() and item.suffix not in (".jsonl", ".mark")
    ]


def _run(raw: bytes, tmp_path: Path) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["SHIM_GUARD_CONFIG"] = str(tmp_path / "config.toml")
    environment["TMPDIR"] = str(tmp_path)
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
            "sessionId": "session",
            "timestamp": 1,
            "cwd": "/workspace",
            "prompt": prompt,
            "transformedPrompt": prompt,
        },
        separators=(",", ":"),
    ).encode()


def test_copilot_runner_allows_safe_prompts_silently(tmp_path: Path) -> None:
    result = _run(_payload("Explain merge sort."), tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


def test_copilot_runner_rewrites_sensitive_prompts_without_a_file(
    tmp_path: Path,
) -> None:
    result = _run(_payload("Contact alice@example.com"), tmp_path)

    assert result.returncode == 0
    assert result.stderr == b""
    assert json.loads(result.stdout) == {
        "modifiedTransformedPrompt": "Contact <EMAIL_1>"
    }
    assert b"alice@example.com" not in result.stdout
    assert not _redaction_files(tmp_path)


def test_copilot_observe_mode_does_not_rewrite_the_prompt(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[mode]\nuser-prompt = "observe"\n', encoding="utf-8")

    result = _run(_payload("Contact alice@example.com"), tmp_path)

    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    assert _redaction_files(tmp_path) == [config]


def test_copilot_runner_replaces_invalid_input(tmp_path: Path) -> None:
    result = _run(b'{"prompt":"hello"}', tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        GENERIC_REWRITE,
        b"",
    )


def test_copilot_stdin_deadline_uses_the_native_rewrite() -> None:
    code = (
        "import sys; from shim_cli import hook as runner; "
        "sys.argv.append('copilot'); runner.HOOK_DEADLINE_SECONDS = 0.05; "
        "runner.main()"
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
        assert process.stdout.read() == GENERIC_REWRITE
        assert process.stderr.read() == b""
