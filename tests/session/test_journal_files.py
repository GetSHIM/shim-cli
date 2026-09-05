import os
import subprocess
import sys

import pytest

from shim_guard.session import _files


def test_short_writes_are_completed_and_failed_writes_rolled_back(
    tmp_path, monkeypatch
):
    root = _files.open_root(tmp_path / "journal")
    write = os.write
    try:
        monkeypatch.setattr(os, "write", lambda fd, data: write(fd, data[:2]))
        assert _files.append(root, "entries", b"first\n", 100)
        assert _files.read(root, "entries", 100) == b"first\n"
        calls = 0

        def fail_after_prefix(fd, data):
            nonlocal calls
            calls += 1
            return write(fd, data[:2]) if calls == 1 else 0

        monkeypatch.setattr(os, "write", fail_after_prefix)
        with pytest.raises(OSError):
            _files.append(root, "entries", b"second\n", 100)
        assert _files.read(root, "entries", 100) == b"first\n"
    finally:
        os.close(root)


@pytest.mark.parametrize("unsafe", ["permissions", "hardlink", "fifo", "symlink"])
def test_unsafe_existing_journal_is_refused(tmp_path, unsafe):
    path = tmp_path / "journal"
    root = _files.open_root(path)
    target = path / "entries"
    try:
        if unsafe == "fifo":
            os.mkfifo(target, 0o600)
        elif unsafe == "symlink":
            target.symlink_to(path / "missing")
        else:
            target.write_bytes(b"keep\n")
            target.chmod(0o644 if unsafe == "permissions" else 0o600)
            if unsafe == "hardlink":
                os.link(target, path / "linked")
        with pytest.raises(OSError):
            _files.append(root, "entries", b"new\n", 100)
        with pytest.raises(OSError):
            _files.read(root, "entries", 100)
    finally:
        os.close(root)


def test_processes_cannot_overrun_cap_or_interleave_records(tmp_path):
    path = tmp_path / "journal"
    root = _files.open_root(path)
    code = """
import os, sys
from pathlib import Path
from shim_guard.session import _files
root = _files.open_root(Path(sys.argv[1]))
count = 0
try:
    for _ in range(100):
        try:
            count += _files.append(root, "entries", b"0123456789\\n", 110)
        except BlockingIOError:
            pass
    print(count)
finally:
    os.close(root)
"""
    try:
        children = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(4)
        ]
        written = 0
        for child in children:
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stderr
            written += int(stdout)
        content = _files.read(root, "entries", 1000)
        assert len(content) <= 110
        assert content == b"0123456789\n" * written
        assert written == 10
    finally:
        os.close(root)
