from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
# `uses: owner/repo@<40 hex> # <version>` — a tag can be moved, a commit cannot.
PINNED = re.compile(r"uses:\s*\S+@([0-9a-f]{40})\s*#\s*\S+")
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)", re.MULTILINE)
EXPECTED = {"ci.yml", "codeql.yml", "prose.yml", "release.yml", "scorecard.yml"}


def _files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml"))


def test_the_repository_runs_the_checks_a_reviewer_looks_for() -> None:
    assert {path.name for path in _files()} >= EXPECTED
    assert (ROOT / ".github" / "dependabot.yml").is_file()
    assert (ROOT / ".github" / "codeql" / "codeql-config.yml").is_file()


@pytest.mark.parametrize("workflow", _files(), ids=lambda path: path.name)
def test_every_action_is_pinned_to_a_commit_with_its_version(workflow: Path) -> None:
    text = workflow.read_text(encoding="utf-8")
    loose = [
        used
        for used in USES.findall(text)
        if "@" not in used or not re.fullmatch(r"[0-9a-f]{40}", used.split("@")[-1])
    ]

    assert not loose, f"{workflow.name}: not pinned to a commit: {loose}"
    assert len(PINNED.findall(text)) == len(USES.findall(text)), (
        f"{workflow.name}: a pin is missing its version comment"
    )


@pytest.mark.parametrize("workflow", _files(), ids=lambda path: path.name)
def test_a_workflow_starts_from_least_privilege(workflow: Path) -> None:
    text = workflow.read_text(encoding="utf-8")

    assert re.search(r"^permissions:", text, re.MULTILINE), (
        f"{workflow.name}: no top-level permissions block"
    )
    checkouts = text.count("uses: actions/checkout@")
    assert text.count("persist-credentials: false") == checkouts, (
        f"{workflow.name}: a checkout keeps its credentials"
    )
