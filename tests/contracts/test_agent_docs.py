"""context7.json and llms.txt point agents at real documents and real commands.

Without a context7.json the index generates examples from source code and
teaches internal functions as the interface; a link or command that has
drifted teaches something that does not exist.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import click
from typer.main import get_command

from shim_cli.cli.app import app

ROOT = Path(__file__).resolve().parents[2]
BLOB = "https://github.com/GetSHIM/shim-cli/blob/main/"
SCHEMA_KEYS = {
    "$schema",
    "projectTitle",
    "description",
    "branch",
    "folders",
    "excludeFolders",
    "excludeFiles",
    "rules",
    "disallow",
    "redirect",
    "previousVersions",
    "url",
    "public_key",
}


def _tracked() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return listed.stdout.splitlines()


def _commands(group: click.Group, prefix: str = "shim") -> set[str]:
    found = set()
    for name, command in group.commands.items():
        found.add(f"{prefix} {name}")
        if isinstance(command, click.Group):
            found |= _commands(command, f"{prefix} {name}")
    return found


CONTEXT7 = json.loads((ROOT / "context7.json").read_text(encoding="utf-8"))


def test_context7_uses_only_schema_keys() -> None:
    assert set(CONTEXT7) <= SCHEMA_KEYS
    assert CONTEXT7["projectTitle"] == "shim-cli"


def test_every_exclusion_names_something_tracked() -> None:
    tracked = _tracked()
    folders = {str(Path(path).parent) for path in tracked}
    folders |= {str(parent) for path in tracked for parent in Path(path).parents}
    names = {Path(path).name for path in tracked}

    assert set(CONTEXT7["excludeFolders"]) <= folders
    assert set(CONTEXT7["excludeFiles"]) <= names


def test_every_command_a_rule_names_is_a_real_command() -> None:
    named = {
        command
        for rule in CONTEXT7["rules"]
        for command in re.findall(r"`(shim [a-z][a-z -]*?)`", rule)
    }
    commands = _commands(get_command(app))

    assert named
    assert named <= commands


def test_every_llms_txt_link_is_a_tracked_file_on_main() -> None:
    text = (ROOT / "llms.txt").read_text(encoding="utf-8")
    links = re.findall(r"\]\(([^)]+)\)", text)
    tracked = set(_tracked())

    assert text.startswith("# shim-cli\n\n> ")
    assert links
    for link in links:
        assert link.startswith(BLOB), link
        assert link[len(BLOB) :] in tracked, link


def test_llms_txt_names_the_newest_release_notes() -> None:
    text = (ROOT / "llms.txt").read_text(encoding="utf-8")
    newest = max(
        (path.stem for path in (ROOT / "docs" / "releases").glob("*.md")),
        key=lambda version: tuple(map(int, version.split("."))),
    )

    assert f"[{newest} release notes]({BLOB}docs/releases/{newest}.md)" in text
