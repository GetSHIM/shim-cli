import fcntl
import os
import subprocess
import sys
import threading
import time
import types

import pytest

from shim_cli.session import _files


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
from shim_cli.session import _files
root = _files.open_root(Path(sys.argv[1]))
count = 0
try:
    for _ in range(100):
        count += _files.append(root, "entries", b"0123456789\\n", 110)
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


def _held(root: int, name: str, mode: int, seconds: float) -> threading.Thread:
    """Hold a lock on `name` the way an append or a read in flight does, then
    let go: a thread, because a lock belongs to its open file description."""
    descriptor = os.open(name, os.O_RDWR | os.O_CREAT, 0o600, dir_fd=root)
    fcntl.flock(descriptor, mode)

    def release() -> None:
        time.sleep(seconds)
        os.close(descriptor)

    thread = threading.Thread(target=release)
    thread.start()
    return thread


def test_concurrent_processes_each_land_their_record_under_a_held_lock(tmp_path):
    path = tmp_path / "journal"
    root = _files.open_root(path)
    # Each child says when it first finds the lock held and pauses to retry.
    code = """
import os, sys, time, types
from pathlib import Path
from shim_cli.session import _files
root = _files.open_root(Path(sys.argv[1]))
waited = []
def pause(seconds):
    if not waited:
        waited.append(True)
        print("waiting", flush=True)
    time.sleep(seconds)
_files.time = types.SimpleNamespace(monotonic=time.monotonic, sleep=pause)
print("ready", flush=True)
sys.stdin.readline()
try:
    print(_files.append(root, "entries", sys.argv[2].encode() + b"\\n", 1000))
finally:
    os.close(root)
"""
    children = []
    try:
        for index in range(6):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", code, str(path), f"record-{index}"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        for child in children:
            assert child.stdout.readline() == b"ready\n"
        # The first child finds the lock held; the rest are let go as it is
        # released, so they meet each other's appends: no timer decides it.
        holder = os.open("entries", os.O_RDWR | os.O_CREAT, 0o600, dir_fd=root)
        fcntl.flock(holder, fcntl.LOCK_EX)
        first, *rest = children
        first.stdin.write(b"go\n")
        first.stdin.flush()
        assert first.stdout.readline() == b"waiting\n"
        for child in rest:
            child.stdin.write(b"go\n")
            child.stdin.flush()
        os.close(holder)
        for child in children:
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stderr
            assert stdout.endswith(b"True\n")
        content = _files.read(root, "entries", 1000)
        assert sorted(content.splitlines()) == [
            f"record-{index}".encode() for index in range(6)
        ]
    finally:
        for child in children:
            child.kill()
            child.communicate()
        os.close(root)


def _let_go_while_waited_for(
    monkeypatch, root: int, name: str, mode: int
) -> list[bool]:
    """Hold a lock on `name` and let go of it when the waiter, having found it
    held, pauses before trying again: the order a released lock is met in, kept
    whatever a busy machine does to a thread's sleep."""
    descriptor = os.open(name, os.O_RDWR | os.O_CREAT, 0o600, dir_fd=root)
    fcntl.flock(descriptor, mode)
    released: list[bool] = []

    def pause(seconds: float) -> None:
        if not released:
            os.close(descriptor)
            released.append(True)
        time.sleep(seconds)

    monkeypatch.setattr(
        _files, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=pause)
    )
    return released


def test_a_reader_waits_for_an_append_in_flight(tmp_path, monkeypatch):
    root = _files.open_root(tmp_path / "journal")
    try:
        assert _files.append(root, "entries", b"first\n", 100)
        released = _let_go_while_waited_for(monkeypatch, root, "entries", fcntl.LOCK_EX)

        assert _files.read(root, "entries", 100) == b"first\n"
        assert released == [True]
    finally:
        os.close(root)


def test_a_writer_waits_for_a_reader(tmp_path, monkeypatch):
    root = _files.open_root(tmp_path / "journal")
    try:
        released = _let_go_while_waited_for(monkeypatch, root, "entries", fcntl.LOCK_SH)

        assert _files.append(root, "entries", b"first\n", 100) is True
        assert released == [True]
        assert _files.read(root, "entries", 100) == b"first\n"
    finally:
        os.close(root)


def test_a_lock_that_is_never_released_is_given_up_after_a_bound(tmp_path):
    root = _files.open_root(tmp_path / "journal")
    try:
        holder = _held(root, "entries", fcntl.LOCK_EX, 1.0)
        started = time.monotonic()

        with pytest.raises(BlockingIOError):
            _files.append(root, "entries", b"first\n", 100)

        assert 0.05 < time.monotonic() - started < 0.9
        holder.join()
    finally:
        os.close(root)
