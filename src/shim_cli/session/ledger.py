from __future__ import annotations

import contextlib
import datetime
import json
import os
from pathlib import Path

from shim_cli import json_bytes

from . import _files, spool

RETENTION_DAYS = 30
MAX_LEDGER_BYTES = 5_000_000
FILE_PREFIX = "ledger-"
FILE_SUFFIX = ".jsonl"
# An empty file beside a month, left when an event could not be appended to it.
_LOST_SUFFIX = ".lost"


class LedgerError(RuntimeError):
    pass


def _root_for(directory: str) -> Path:
    if xdg := os.environ.get("XDG_STATE_HOME"):
        root = Path(xdg).expanduser() / directory
    else:
        try:
            root = Path.home() / ".local" / "state" / directory
        except RuntimeError as error:
            raise LedgerError("ledger directory is invalid") from error
    if not root.is_absolute() or ".." in root.parts:
        raise LedgerError("ledger directory is invalid")
    return root


def root_path() -> Path:
    return _root_for("shim")


def legacy_root_path() -> Path | None:
    """The 0.2.0 directory."""
    try:
        return _root_for("shim-guard")
    except LedgerError:
        return None


def _month(when: datetime.datetime) -> str:
    return f"{FILE_PREFIX}{when.year:04d}-{when.month:02d}{FILE_SUFFIX}"


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
            for path in root_path().glob(f"{FILE_PREFIX}*{FILE_SUFFIX}")
            if not path.is_symlink() and path.is_file()
        )
    except OSError as error:
        raise LedgerError("ledger could not be listed") from error
    finally:
        os.close(root)


def _month_end(path: Path) -> datetime.datetime | None:
    stem = path.name[len(FILE_PREFIX) : -len(FILE_SUFFIX)]
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
            path.with_suffix(_LOST_SUFFIX).unlink(missing_ok=True)
            path.unlink()
            removed += 1
    return removed


def append(entry: dict, now: datetime.datetime | None = None) -> bool:
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    line = json_bytes.dumps(entry, sort_keys=True) + b"\n"
    prune(moment)
    root = _open_root()
    try:
        return _files.append(root, _month(moment), line, MAX_LEDGER_BYTES)
    except OSError as error:
        _files.mark_lost(root, Path(_month(moment)).with_suffix(_LOST_SUFFIX).name)
        raise LedgerError("ledger could not be written") from error
    finally:
        os.close(root)


def probe(now: datetime.datetime | None = None) -> None:
    """Raise `LedgerError` unless an event could be appended now: the folder
    opens (and is created), and this month's file, if there, is private and
    regular. Writes nothing."""
    root = _open_root()
    try:
        descriptor = os.open(
            _month(now or datetime.datetime.now(datetime.timezone.utc)),
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=root,
        )
        try:
            _files.validate(descriptor)
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise LedgerError("ledger file is not private and regular") from error
    finally:
        os.close(root)


def capped() -> bool:
    """True when a retained month is known to be short: it holds no room for
    another record, or an event could not be appended. What the ledger shows
    ends before the events do, and it reads every retained month."""
    for path in files():
        if path.with_suffix(_LOST_SUFFIX).exists():
            return True
        try:
            size = path.stat().st_size
        except OSError as error:
            raise LedgerError("ledger could not be read") from error
        if size + spool.MAX_ENTRY_BYTES > MAX_LEDGER_BYTES:
            return True
    return False


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
    # A migrated month holds the old file's lines after the new file's, because
    # the merge is an append. Chronology comes from the timestamps; the sort is
    # stable, so entries without one keep their position.
    found.sort(key=lambda entry: str(entry.get("ts", "")))
    return found


def purge() -> int:
    removed = 0
    for path in files():
        with contextlib.suppress(OSError):
            path.with_suffix(_LOST_SUFFIX).unlink(missing_ok=True)
            path.unlink()
            removed += 1
    return removed


__all__ = [
    "MAX_LEDGER_BYTES",
    "RETENTION_DAYS",
    "LedgerError",
    "append",
    "capped",
    "entries",
    "files",
    "probe",
    "prune",
    "purge",
    "root_path",
]
