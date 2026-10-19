"""Private journal descriptors, bounded reads, and capped writes. A lock is
waited for briefly, never indefinitely: the hook has a deadline."""

import contextlib
import fcntl
import os
import stat
import time
from pathlib import Path

_CREATE_ATTEMPTS = 3
# A lock is held for the length of one append or one read, microseconds to a
# few milliseconds, so a collision between parallel hooks is waited out here
# instead of dropping the record.
LOCK_WAIT_SECONDS = 0.1
_LOCK_POLL_SECONDS = 0.001


def open_root(path: Path) -> int:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise OSError("journal directory is not private")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def validate(descriptor: int) -> os.stat_result:
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise OSError("journal file is not private and regular")
    return info


def lock(descriptor: int, kind: int) -> None:
    """Take `LOCK_SH` or `LOCK_EX`; BlockingIOError once the wait is over."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(descriptor, kind | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_LOCK_POLL_SECONDS)


def read(root: int, name: str, limit: int) -> bytes:
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root
        )
    except FileNotFoundError:
        return b""
    try:
        validate(descriptor)
        lock(descriptor, fcntl.LOCK_SH)
        result = bytearray()
        while len(result) < limit:
            chunk = os.read(descriptor, min(65_536, limit - len(result)))
            if not chunk:
                break
            result.extend(chunk)
        return bytes(result)
    finally:
        os.close(descriptor)


def _open_for_append(root: int, name: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
    for _ in range(_CREATE_ATTEMPTS - 1):
        try:
            return os.open(name, flags, 0o600, dir_fd=root)
        except FileNotFoundError:
            # Darwin returns ENOENT when concurrent O_CREAT opens race.
            pass
    return os.open(name, flags, 0o600, dir_fd=root)


def append(root: int, name: str, line: bytes, limit: int) -> bool:
    descriptor = _open_for_append(root, name)
    try:
        validate(descriptor)
        lock(descriptor, fcntl.LOCK_EX)
        size = os.fstat(descriptor).st_size
        if size + len(line) > limit:
            return False
        try:
            view = memoryview(line)
            while view:
                written = os.write(descriptor, view)
                if not written:
                    raise OSError("journal write made no progress")
                view = view[written:]
        except OSError:
            os.ftruncate(descriptor, size)
            raise
        return True
    finally:
        os.close(descriptor)


def mark_lost(root: int, name: str) -> None:
    """Leave an empty `name` beside a journal: a record could not be appended,
    and the file's existence is all that says so."""
    with contextlib.suppress(OSError):
        os.close(
            os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=root,
            )
        )
