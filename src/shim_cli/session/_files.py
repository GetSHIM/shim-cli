"""Private journal descriptors, bounded reads, and non-blocking capped writes."""

import fcntl
import os
import stat
from pathlib import Path

_CREATE_ATTEMPTS = 3


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


def read(root: int, name: str, limit: int) -> bytes:
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root
        )
    except FileNotFoundError:
        return b""
    try:
        validate(descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
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
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
