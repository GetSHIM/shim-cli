from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from shim_cli.cli import privacy

TURKISH = "Müşteri şifre ğüİı: jane.doe@example.com\n"


@pytest.mark.parametrize("encoding", ("iso8859-9", "latin-1"))
def test_redact_to_a_pipe_keeps_utf8_whatever_the_locale(
    encoding: str, tmp_path: Path
) -> None:
    # The conftest already points HOME, XDG and TMPDIR at tmp_path.
    result = subprocess.run(
        [sys.executable, "-c", "from shim_cli.cli.app import main; main()", "redact"],
        input=TURKISH.encode("utf-8"),
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, "LC_ALL": "C", "PYTHONIOENCODING": encoding},
        check=False,
    )

    assert (result.returncode, result.stderr) == (0, b"")
    assert result.stdout == "Müşteri şifre ğüİı: <EMAIL_1>\n".encode()


@pytest.mark.parametrize(
    ("encoding", "shown"),
    (
        ("utf-8", "Müşteri 😀 x\\x1b[2J <EMAIL_1>\n"),
        ("iso8859-9", "Müşteri \\U0001f600 x\\x1b[2J <EMAIL_1>\n"),
        ("latin-1", "Mü\\u015fteri \\U0001f600 x\\x1b[2J <EMAIL_1>\n"),
    ),
    ids=("utf-8", "iso8859-9", "latin-1"),
)
def test_redact_on_a_terminal_escapes_what_the_terminal_cannot_show(
    encoding: str, shown: str, monkeypatch
) -> None:
    screen = io.BytesIO()
    terminal = io.TextIOWrapper(screen, encoding=encoding)
    monkeypatch.setattr(terminal, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdout", terminal)
    source = "Müşteri 😀 x\x1b[2J jane.doe@example.com\n".encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(source)))

    privacy.redact(as_json=False)

    terminal.flush()
    assert screen.getvalue() == shown.encode(encoding)
