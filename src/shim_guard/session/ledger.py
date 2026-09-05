from __future__ import annotations

import contextlib
import datetime
import json
import os
from pathlib import Path

from . import _files

RETENTION_DAYS = 30
MAX_LEDGER_BYTES = 5_000_000
_PREFIX = "ledger-"
_SUFFIX = ".jsonl"


class LedgerError(RuntimeError):
    pass


def root_path() -> Path:
    configured = os.environ.get("SHIM_GUARD_STATE_DIR")
    if configured:
        root = Path(configured).expanduser()
    elif xdg := os.environ.get("XDG_STATE_HOME"):
        root = Path(xdg).expanduser() / "shim-guard"
    else:
        try:
            root = Path.home() / ".local" / "state" / "shim-guard"
        except RuntimeError as error:
            raise LedgerError("ledger directory is invalid") from error
    if not root.is_absolute() or ".." in root.parts:
        raise LedgerError("ledger directory is invalid")
    return root


def _month(when: datetime.datetime) -> str:
    return f"{_PREFIX}{when.year:04d}-{when.month:02d}{_SUFFIX}"


def _open_root() -> int:
    try:
        return _files.open_root(root_path())
    except OSError as error:
        raise LedgerError("ledger directory could not be opened safely") from error


def files() -> list:
    if not root_path().exists():
        return []
    root = _open_root()
    try:
        return sorted(
            path
            for path in root_path().glob(f"{_PREFIX}*{_SUFFIX}")
            if not path.is_symlink() and path.is_file()
        )
    except OSError as error:
        raise LedgerError("ledger could not be listed") from error
    finally:
        os.close(root)


def _month_end(path: Path) -> datetime.datetime | None:
    stem = path.name[len(_PREFIX) : -len(_SUFFIX)]
    try:
        year, month = (int(part) for part in stem.split("-", 1))
        start = datetime.datetime(year, month, 1, tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return (start + datetime.timedelta(days=31)).replace(day=1)


def prune(now: datetime.datetime | None = None) -> int:
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    removed = 0
    for path in files():
        end = _month_end(path)
        if end is None or end + datetime.timedelta(days=RETENTION_DAYS) > moment:
            continue
        with contextlib.suppress(OSError):
            path.unlink()
            removed += 1
    return removed


def append(entry: dict, now: datetime.datetime | None = None) -> bool:
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True).encode() + b"\n"
    prune(moment)
    root = _open_root()
    try:
        return _files.append(root, _month(moment), line, MAX_LEDGER_BYTES)
    except OSError as error:
        raise LedgerError("ledger could not be written") from error
    finally:
        os.close(root)


def entries(since: datetime.datetime | None = None) -> list:
    found = []
    boundary = since.isoformat().replace("+00:00", "Z") if since else ""
    for path in files():
        try:
            root = _open_root()
            try:
                content = _files.read(root, path.name, MAX_LEDGER_BYTES)
            finally:
                os.close(root)
        except OSError as error:
            raise LedgerError("ledger could not be read") from error
        for line in content.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if boundary and str(entry.get("ts", "")) < boundary:
                continue
            found.append(entry)
    return found


def purge() -> int:
    removed = 0
    for path in files():
        with contextlib.suppress(OSError):
            path.unlink()
            removed += 1
    return removed


__all__ = [
    "MAX_LEDGER_BYTES",
    "RETENTION_DAYS",
    "LedgerError",
    "append",
    "entries",
    "files",
    "prune",
    "purge",
    "root_path",
]
