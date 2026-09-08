from __future__ import annotations

import fcntl
import os
import secrets
import stat
from pathlib import Path

from .plan import Action, FileState, Plan, StateKind

MAX_PATH_BYTES = 4_096
_ANCESTOR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
_UNSAFE_WRITABLE = stat.S_IWGRP | stat.S_IWOTH


class InstallationError(RuntimeError):
    pass


def _validate_target(target: Path) -> None:
    if (
        not target.is_absolute()
        or ".." in target.parts
        or not str(target).isprintable()
    ):
        raise InstallationError("target must be an absolute normalized path")
    if not target.name:
        raise InstallationError("target must name a file")
    if len(os.fsencode(target)) > MAX_PATH_BYTES:
        raise InstallationError("target path is too long")


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _unsafe_reason(info: os.stat_result, label: str) -> str:
    if info.st_uid != os.geteuid():
        return f"{label} is not owned by the current user"
    if info.st_mode & _UNSAFE_WRITABLE:
        return f"{label} is writable by another user"
    return ""


def _open_parent(target: Path) -> int:
    descriptor = os.open("/", _ANCESTOR_FLAGS)
    try:
        for component in target.parent.parts[1:]:
            next_descriptor = os.open(component, _ANCESTOR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError:
        os.close(descriptor)
        raise


def _read_at(target: Path, parent_fd: int, max_bytes: int) -> FileState:
    parent = os.fstat(parent_fd)
    reason = _unsafe_reason(parent, "target parent")
    if reason:
        return FileState(
            StateKind.UNSAFE,
            path=target,
            parent_device=parent.st_dev,
            parent_inode=parent.st_ino,
            max_bytes=max_bytes,
            reason=reason,
        )
    try:
        info = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return FileState(
            StateKind.ABSENT,
            path=target,
            parent_device=parent.st_dev,
            parent_inode=parent.st_ino,
            max_bytes=max_bytes,
        )
    except OSError as error:
        return FileState(
            StateKind.UNSAFE,
            path=target,
            parent_device=parent.st_dev,
            parent_inode=parent.st_ino,
            max_bytes=max_bytes,
            reason=f"cannot inspect target: {error.strerror}",
        )
    fingerprint = _fingerprint(info)

    def unsafe(message: str) -> FileState:
        return FileState(
            StateKind.UNSAFE,
            path=target,
            parent_device=parent.st_dev,
            parent_inode=parent.st_ino,
            fingerprint=fingerprint,
            mode=stat.S_IMODE(info.st_mode),
            max_bytes=max_bytes,
            reason=message,
        )

    if stat.S_ISLNK(info.st_mode):
        return unsafe("target must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        return unsafe("target must be a regular file")
    if info.st_nlink != 1:
        return unsafe("target must not be hard-linked")
    if reason := _unsafe_reason(info, "target"):
        return unsafe(reason)
    if info.st_size > max_bytes:
        return unsafe("target exceeds the inspection limit")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(target.name, flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            content = bytearray()
            while len(content) <= max_bytes:
                chunk = os.read(descriptor, min(65_536, max_bytes + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            closed = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        return unsafe(f"cannot safely read target: {error.strerror}")
    if len(content) > max_bytes:
        return unsafe("target exceeds the inspection limit")
    opened_fingerprint = _fingerprint(opened)
    if fingerprint != opened_fingerprint or opened_fingerprint != _fingerprint(closed):
        return unsafe("target changed during inspection")
    return FileState(
        StateKind.FILE,
        path=target,
        content=bytes(content),
        parent_device=parent.st_dev,
        parent_inode=parent.st_ino,
        fingerprint=fingerprint,
        mode=stat.S_IMODE(info.st_mode),
        max_bytes=max_bytes,
    )


def inspect_file(path: Path, max_bytes: int = 1_000_000) -> FileState:
    target = Path(path)
    try:
        _validate_target(target)
        if max_bytes < 0:
            raise InstallationError("inspection limit must not be negative")
        parent_fd = _open_parent(target)
    except FileNotFoundError:
        return FileState(
            StateKind.ABSENT,
            path=target,
            max_bytes=max_bytes,
            reason="target parent does not exist",
        )
    except (InstallationError, OSError) as error:
        detail = str(error) if isinstance(error, InstallationError) else error.strerror
        return FileState(
            StateKind.UNSAFE,
            path=target,
            max_bytes=max_bytes,
            reason=detail or "unsafe path",
        )
    try:
        return _read_at(target, parent_fd, max_bytes)
    finally:
        os.close(parent_fd)


def ensure_parent(path: Path) -> None:
    target = Path(path)
    _validate_target(target)
    descriptor = -1
    try:
        descriptor = os.open("/", _ANCESTOR_FLAGS)
        for component in target.parent.parts[1:]:
            try:
                next_descriptor = os.open(component, _ANCESTOR_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if reason := _unsafe_reason(os.fstat(descriptor), "target ancestor"):
                    raise InstallationError(reason) from None
                os.mkdir(component, 0o700, dir_fd=descriptor)
                next_descriptor = os.open(component, _ANCESTOR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        if reason := _unsafe_reason(os.fstat(descriptor), "target parent"):
            raise InstallationError(reason)
    except OSError as error:
        raise InstallationError(
            f"cannot create target parent safely: {error.strerror or error}"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _path_matches_parent(target: Path, parent_fd: int) -> bool:
    try:
        current_fd = _open_parent(target)
    except OSError:
        return False
    try:
        current = os.fstat(current_fd)
        locked = os.fstat(parent_fd)
        return (current.st_dev, current.st_ino) == (locked.st_dev, locked.st_ino)
    finally:
        os.close(current_fd)


def _require_planned_state(plan: Plan, parent_fd: int) -> None:
    if not _path_matches_parent(plan.target, parent_fd):
        raise InstallationError("target path changed after planning")
    current = _read_at(plan.target, parent_fd, plan.state.max_bytes)
    if current != plan.state:
        raise InstallationError(current.reason or "target changed after planning")


def _open_locked_parent(plan: Plan) -> int:
    descriptor = -1
    try:
        descriptor = _open_parent(plan.target)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        parent = os.fstat(descriptor)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise InstallationError(
            f"cannot lock target parent: {error.strerror}"
        ) from error
    if (parent.st_dev, parent.st_ino) != (
        plan.state.parent_device,
        plan.state.parent_inode,
    ):
        os.close(descriptor)
        raise InstallationError("target parent changed after planning")
    return descriptor


def _temporary(plan: Plan, parent_fd: int) -> tuple[str, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(10):
        name = f".{plan.target.name}.shim-{secrets.token_hex(8)}"
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
            return name, descriptor
        except FileExistsError:
            pass
    raise InstallationError("could not allocate a safe temporary file")


def _clean_temporary(name: str, descriptor: int, parent_fd: int) -> None:
    written = os.fstat(descriptor)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (written.st_dev, written.st_ino):
        os.unlink(name, dir_fd=parent_fd)


def _publish(plan: Plan, parent_fd: int) -> None:
    assert plan.expected is not None
    temporary, descriptor = _temporary(plan, parent_fd)
    published = False
    try:
        mode = plan.state.mode if plan.action is Action.UPDATE else 0o600
        assert mode is not None
        if plan.action is Action.UPDATE:
            assert plan.state.fingerprint is not None
            os.fchown(descriptor, -1, plan.state.fingerprint[5])
        view = memoryview(plan.expected)
        while view:
            written = os.write(descriptor, view)
            if not written:
                raise InstallationError("could not write temporary file")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        temporary_info = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if (
            (temporary_info.st_dev, temporary_info.st_ino)
            != (opened.st_dev, opened.st_ino)
            or temporary_info.st_nlink != 1
            or opened.st_nlink != 1
        ):
            raise InstallationError("temporary file changed before publication")
        _require_planned_state(plan, parent_fd)
        # ponytail: POSIX lacks conditional replace; add CAS for same-UID threats.
        if plan.action is Action.CREATE:
            try:
                os.link(
                    temporary,
                    plan.target.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise InstallationError("target appeared during publication") from error
            _clean_temporary(temporary, descriptor, parent_fd)
        else:
            os.replace(
                temporary,
                plan.target.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        published_info = os.stat(
            plan.target.name, dir_fd=parent_fd, follow_symlinks=False
        )
        if (published_info.st_dev, published_info.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ) or published_info.st_nlink != 1:
            raise InstallationError("target changed during publication")
        published = True
        os.fsync(parent_fd)
    finally:
        try:
            if not published:
                _clean_temporary(temporary, descriptor, parent_fd)
        finally:
            os.close(descriptor)


def apply(plan: Plan) -> bool:
    if plan.action in {Action.CONFLICT, Action.REFUSE}:
        raise InstallationError(plan.message)
    parent_fd = _open_locked_parent(plan)
    try:
        _require_planned_state(plan, parent_fd)
        if plan.action is Action.NOOP:
            return False
        _publish(plan, parent_fd)
        return True
    finally:
        fcntl.flock(parent_fd, fcntl.LOCK_UN)
        os.close(parent_fd)
