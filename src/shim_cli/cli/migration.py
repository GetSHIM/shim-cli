"""One-way moves off the 0.2.0 names, done by the commands that already run.

The hook never migrates: it reads the new location and falls back to the old
one. There is no marker file and no "already migrated" state — the absence of
the old path is the state, so each move is reported exactly once.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

from shim_cli import config
from shim_cli.cli.output import emit
from shim_cli.session import _files, ledger


def _move(source: Path, target: Path) -> None:
    try:
        os.replace(source, target)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        # A separate mount for the state or config home defeats os.replace.
        shutil.copy2(source, target)
        source.unlink()


def _merge_ledger(source: Path, destination: Path) -> bool:
    """Same month on both sides of the rename: keep both files' lines.

    The hook writes the new file as soon as it runs, so an upgrade usually
    reaches this rather than the plain move. Skipping on collision, as 0.3.0
    did, left the old file on disk and doctor warning about it forever.

    One locked append of the whole blob, not a line at a time: the file is
    append-only JSONL, so a single `O_APPEND` write under the same lock the
    hook takes cannot interleave with it.
    """
    try:
        blob = source.read_bytes()[: ledger.MAX_LEDGER_BYTES]
    except OSError:
        return False
    if blob and not blob.endswith(b"\n"):
        blob += b"\n"
    if not blob.strip():
        with contextlib.suppress(OSError):
            source.unlink()
        return True
    try:
        root = _files.open_root(destination.parent)
    except OSError:
        return False
    try:
        # False means the merge would cross the size cap; leaving the old file
        # in place loses nothing and doctor keeps pointing at it.
        if not _files.append(root, destination.name, blob, ledger.MAX_LEDGER_BYTES):
            return False
    except OSError:
        return False
    finally:
        os.close(root)
    with contextlib.suppress(OSError):
        source.unlink()
    return True


def settings() -> tuple[str, ...]:
    legacy = config.legacy_config_path()
    if legacy is None or not legacy.is_file():
        return ()
    try:
        target = config.config_path()
    except ValueError:
        return ()
    if target.exists():
        return ()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _move(legacy, target)
    return (f"moved settings to {target}",)


def ledger_files() -> tuple[str, ...]:
    legacy = ledger.legacy_root_path()
    if legacy is None or not legacy.is_dir():
        return ()
    try:
        target = ledger.root_path()
    except ledger.LedgerError:
        return ()
    moved = 0
    for path in sorted(legacy.glob(f"{ledger.FILE_PREFIX}*{ledger.FILE_SUFFIX}")):
        destination = target / path.name
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.exists():
            if not _merge_ledger(path, destination):
                continue
        else:
            _move(path, destination)
        moved += 1
    with contextlib.suppress(OSError):
        legacy.rmdir()
    if not moved:
        return ()
    plural = "" if moved == 1 else "s"
    return (f"moved {moved} ledger file{plural} to {target}",)


def announce(notes: Sequence[str], *, as_json: bool) -> None:
    """JSON output owns stdout, so a move is reported on stderr in that mode."""
    for note in notes:
        emit("PASS", note, error=as_json)
