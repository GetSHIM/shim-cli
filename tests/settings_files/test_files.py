from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from shim_cli.settings_files import (
    Action,
    InstallationError,
    StateKind,
    apply,
    ensure_parent,
    inspect_file,
    plan_change,
)


def target_in(tmp_path: Path) -> Path:
    parent = tmp_path / ".config"
    parent.mkdir(mode=0o700)
    return parent / "shared.json"


def plan(target: Path, expected: bytes | None, conflict: str = ""):
    return plan_change(target, inspect_file(target), expected, conflict)


def test_create_update_and_noop(tmp_path: Path) -> None:
    target = target_in(tmp_path)

    create = plan(target, b"first\n")
    assert create.action is Action.CREATE
    assert apply(create)
    assert target.read_bytes() == b"first\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    update = plan(target, b"second\n")
    assert update.action is Action.UPDATE
    assert apply(update)
    assert target.read_bytes() == b"second\n"

    noop = plan(target, b"second\n")
    assert noop.action is Action.NOOP
    assert not apply(noop)


def test_update_preserves_safe_permissions_and_gid(tmp_path: Path) -> None:
    target = target_in(tmp_path)
    target.write_bytes(b"old")
    target.chmod(0o640)
    before = target.stat()

    assert apply(plan(target, b"new"))

    after = target.stat()
    assert stat.S_IMODE(after.st_mode) == 0o640
    assert after.st_gid == before.st_gid


def test_conflict_and_absent_none_do_not_write(tmp_path: Path) -> None:
    target = target_in(tmp_path)
    state = inspect_file(target)
    absent = plan_change(target, state, None)
    assert absent.action is Action.NOOP
    assert not apply(absent)
    assert (
        plan_change(target.with_name("other"), state, b"data").action is Action.REFUSE
    )

    target.write_bytes(b"shared")
    conflict = plan(target, b"replacement", "manual merge required")
    assert conflict.action is Action.CONFLICT
    with pytest.raises(InstallationError, match="manual merge"):
        apply(conflict)
    assert target.read_bytes() == b"shared"


def test_stale_and_content_drift_plans_are_refused(tmp_path: Path) -> None:
    target = target_in(tmp_path)
    stale = plan(target, b"mine")
    target.write_bytes(b"theirs")
    with pytest.raises(InstallationError, match="changed"):
        apply(stale)
    assert target.read_bytes() == b"theirs"

    update = plan(target, b"mine")
    target.write_bytes(b"drift")
    with pytest.raises(InstallationError, match="changed"):
        apply(update)
    assert target.read_bytes() == b"drift"


def test_same_content_on_new_inode_is_refused(tmp_path: Path) -> None:
    target = target_in(tmp_path)
    target.write_bytes(b"old")
    update = plan(target, b"new")
    replacement = target.with_name("replacement")
    replacement.write_bytes(b"old")
    os.replace(replacement, target)

    with pytest.raises(InstallationError, match="changed"):
        apply(update)
    assert target.read_bytes() == b"old"


def test_hardlink_added_before_publish_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shim_cli.settings_files import files

    target = target_in(tmp_path)
    target.write_bytes(b"old")
    update = plan(target, b"new")
    shared = tmp_path / "shared-link"
    real_read = files._read_at
    reads = 0

    def add_link_on_final_read(path: Path, parent_fd: int, max_bytes: int):
        nonlocal reads
        reads += 1
        if reads == 2:
            os.link(target, shared)
        return real_read(path, parent_fd, max_bytes)

    monkeypatch.setattr(files, "_read_at", add_link_on_final_read)
    with pytest.raises(InstallationError, match="hard-linked"):
        apply(update)
    assert target.read_bytes() == b"old"
    assert shared.read_bytes() == b"old"


def test_unsafe_targets_and_limits_are_refused(tmp_path: Path) -> None:
    target = target_in(tmp_path)
    target.write_bytes(b"too large")
    assert inspect_file(target, max_bytes=3).kind is StateKind.UNSAFE

    target.unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"data")
    target.symlink_to(elsewhere)
    assert inspect_file(target).kind is StateKind.UNSAFE

    target.unlink()
    os.link(elsewhere, target)
    assert inspect_file(target).kind is StateKind.UNSAFE

    target.unlink()
    target.mkdir()
    assert inspect_file(target).kind is StateKind.UNSAFE


def test_unsafe_permissions_ownership_and_replacement_size_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = target_in(tmp_path)
    target.parent.chmod(0o770)
    assert inspect_file(target).kind is StateKind.UNSAFE

    target.parent.chmod(0o700)
    target.write_bytes(b"safe")
    target.chmod(0o620)
    assert inspect_file(target).kind is StateKind.UNSAFE

    target.chmod(0o600)
    state = inspect_file(target, max_bytes=4)
    assert plan_change(target, state, b"large").action is Action.REFUSE

    monkeypatch.setattr(os, "geteuid", lambda: target.stat().st_uid + 1)
    assert inspect_file(target).kind is StateKind.UNSAFE


def test_absent_parent_and_symlinked_ancestor_are_handled_safely(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing" / "nested" / "file"
    state = inspect_file(missing)
    assert state.kind is StateKind.ABSENT
    assert plan_change(missing, state, b"data").action is Action.REFUSE
    assert plan_change(missing, state, None).action is Action.NOOP

    ensure_parent(missing)
    ensure_parent(missing)
    assert stat.S_IMODE(missing.parent.stat().st_mode) == 0o700
    assert inspect_file(missing).kind is StateKind.ABSENT
    assert inspect_file(Path("relative")).kind is StateKind.UNSAFE

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    assert inspect_file(linked / "file").kind is StateKind.ABSENT
    ensure_parent(linked / "nested" / "file")
    assert (real / "nested").is_dir()

    exposed = tmp_path / "exposed"
    exposed.mkdir()
    exposed.chmod(0o770)
    to_exposed = tmp_path / "to-exposed"
    to_exposed.symlink_to(exposed, target_is_directory=True)
    assert inspect_file(to_exposed / "file").kind is StateKind.UNSAFE

    victim = real / "victim"
    victim.write_bytes(b"data")
    trap = real / "trap"
    trap.symlink_to(victim)
    assert inspect_file(linked / "trap").kind is StateKind.UNSAFE

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    unsafe.chmod(0o770)
    with pytest.raises(InstallationError, match="writable by another user"):
        ensure_parent(unsafe / "nested" / "file")
