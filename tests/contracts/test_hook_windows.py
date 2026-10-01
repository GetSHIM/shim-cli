from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

NOTICE = b"shim: shim-cli does not support Windows yet; nothing was inspected.\n"
ON_WINDOWS = (
    "import shutil, signal, sys, tempfile; sys.platform = 'win32'; "
    "sys.modules['fcntl'] = None; "
    "del signal.SIGALRM, signal.setitimer, signal.ITIMER_REAL; "
    "from shim_cli.hook import main; main()"
)
PAYLOADS = (
    json.dumps(
        {
            "session_id": "windows",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Contact alice@example.com",
        }
    ).encode(),
    json.dumps(
        {
            "session_id": "windows",
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/work/.env"},
            "tool_response": {"file": {"content": "AKIAIOSFODNN7EXAMPLE"}},
        }
    ).encode(),
    b"not json",
)


@pytest.mark.parametrize("raw", PAYLOADS, ids=("prompt", "tool-event", "malformed"))
@pytest.mark.parametrize("client", ("claude", "codex", "copilot", "vscode"))
def test_on_windows_the_hook_stands_down_with_one_line(
    raw: bytes, client: str, tmp_path: Path
) -> None:
    work = tmp_path / "windows"
    work.mkdir()

    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", ON_WINDOWS, client),
        input=raw,
        capture_output=True,
        check=False,
        timeout=60,
        env={"TMPDIR": str(work), "HOME": str(work)},
    )

    assert (result.returncode, result.stdout, result.stderr) == (0, b"", NOTICE)
    assert not list(work.iterdir())
