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
from shim_cli.session import ledger


def _move(source: Path, target: Path) -> None:
    try:
        os.replace(source, target)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        # A separate mount for the state or config home defeats os.replace.
        shutil.copy2(source, target)
        source.unlink()


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
        if destination.exists():
            continue
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
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
