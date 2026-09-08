"""One interpreter, several names, one hook fragment.

`sys.executable` reports whichever alias started Python. A console script is
launched through `python3`; `python -m shim_cli.hook` reports `python`. Both
name one binary, but a hook fragment is matched as an exact string, so before
this was fixed `shim install` added a second hook next to the first and
`shim doctor` reported the installed one missing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shim_cli.clients.claude import settings as claude
from shim_cli.clients.codex import settings as codex
from shim_cli.clients.copilot import settings as copilot
from shim_cli.clients.hook_settings import interpreter_path


@pytest.fixture
def aliases(tmp_path: Path) -> tuple[Path, ...]:
    """A venv layout: one real interpreter under three names."""
    binaries = tmp_path / "bin"
    binaries.mkdir()
    real = binaries / "python"
    real.write_text("#!/bin/sh\n")
    names = [real]
    for alias in ("python3", "python3.13"):
        link = binaries / alias
        link.symlink_to(real)
        names.append(link)
    return tuple(names)


def test_every_alias_of_one_interpreter_names_the_same_hook(
    aliases: tuple[Path, ...],
) -> None:
    for module in (codex, copilot):
        commands = {module.hook_command(alias) for alias in aliases}
        assert len(commands) == 1, module.__name__
        assert str(aliases[0]) in commands.pop()
    handlers = {claude._handler(alias)["command"] for alias in aliases}
    assert handlers == {str(aliases[0])}


def test_installing_under_a_second_alias_does_not_duplicate_the_hook(
    aliases: tuple[Path, ...],
) -> None:
    installed, other = aliases[0], aliases[1]
    first = codex.add_hook(None, installed)
    again = codex.add_hook(first, other)
    assert again == first
    groups = json.loads(again)["hooks"][codex.PROMPT_EVENT]
    assert len(groups) == 1


def test_a_second_alias_still_removes_the_hook_the_first_installed(
    aliases: tuple[Path, ...],
) -> None:
    installed, other = aliases[0], aliases[1]
    assert codex.remove_hook(codex.add_hook(None, installed), other) == b"{}\n"
    assert claude.remove_hook(claude.add_hook(None, installed), other) == b"{}\n"


def test_an_upgrade_replaces_the_0_2_0_fragment_rather_than_joining_it(
    aliases: tuple[Path, ...],
) -> None:
    """The 0.2.0 install wrote whichever alias it was started with."""
    legacy = codex.add_groups(
        None, codex.hook_groups(aliases[0], codex.LEGACY_HOOK_MODULE)
    )
    upgraded = codex.add_hook(legacy, aliases[1])
    groups = json.loads(upgraded)["hooks"][codex.PROMPT_EVENT]
    assert len(groups) == 1
    assert codex.HOOK_MODULE in groups[0]["hooks"][0]["command"]


def test_an_interpreter_with_no_sibling_alias_keeps_its_own_name(
    tmp_path: Path,
) -> None:
    lonely = tmp_path / "bin" / "python3.13"
    lonely.parent.mkdir()
    lonely.write_text("#!/bin/sh\n")
    assert interpreter_path(lonely) == lonely


def test_an_absent_interpreter_is_left_alone_rather_than_guessed(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "nowhere" / "python3"
    assert interpreter_path(missing) == missing


@pytest.mark.parametrize("bad", ["python3", "relative/python", ""])
def test_a_relative_interpreter_is_still_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        interpreter_path(bad)
