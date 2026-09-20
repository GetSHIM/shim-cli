from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from shim_cli.clients.claude.settings import TESTED_CLAUDE_VERSION
from shim_cli.clients.codex.settings import TESTED_CODEX_VERSION
from shim_cli.clients.copilot.settings import TESTED_COPILOT_VERSION

TESTED_VSCODE_VERSION = "1.137.0"

try:
    import tomllib
except ModuleNotFoundError:  # the 3.10 floor CI also runs
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src/shim_cli", "scripts")
OLD_NAMES = ("SHIM Guard", "shim Guard", "shim_guard")

# The only references that may name the old package: the module spelling each
# client matcher recognises so `install` replaces a 0.2.0 fragment in place.
_LEGACY_MODULE = 'LEGACY_HOOK_MODULE = "shim_guard.hook"'
ALLOWED = {
    "src/shim_cli/clients/claude/settings.py": (_LEGACY_MODULE,),
    "src/shim_cli/clients/codex/settings.py": (_LEGACY_MODULE,),
    "src/shim_cli/clients/copilot/settings.py": (_LEGACY_MODULE,),
}


def _tracked(prefix: str) -> list[str]:
    result = subprocess.run(
        ("git", "ls-files", prefix),
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:  # pragma: no cover - a checkout without git
        pytest.skip("git is unavailable")
    return result.stdout.decode().split()


@pytest.mark.parametrize("prefix", SCANNED)
def test_no_source_file_still_carries_the_old_name(prefix: str) -> None:
    offenders = []
    for relative in _tracked(prefix):
        path = ROOT / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name in OLD_NAMES:
            if name not in text:
                continue
            permitted = ALLOWED.get(relative, ())
            remainder = text
            for allowance in permitted:
                remainder = remainder.replace(allowance, "")
            if name in remainder:
                offenders.append(f"{relative}: {name}")

    assert not offenders, "the old name survives in:\n" + "\n".join(sorted(offenders))


# The release notes are history and keep the names they shipped with.
HISTORY = "docs/releases/"


def test_no_shim_guard_module_script_or_variable_remains() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert _tracked("src/shim_guard") == []
    assert list(pyproject["project"]["scripts"]) == ["shim", "shim-hook"]
    assert pyproject["tool"]["uv"]["build-backend"]["module-name"] == "shim_cli"
    this_file = str(Path(__file__).relative_to(ROOT))
    offenders = []
    for relative in _tracked("."):
        if relative.startswith(HISTORY) or relative == this_file:
            continue
        try:
            text = (ROOT / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        offenders += [
            f"{relative}: {name}"
            for name in ("shim-guard-hook", "SHIM_GUARD_", "shim_guard/")
            if name in text
        ]

    assert not offenders, "\n".join(sorted(offenders))


PROSE_NAMES = ("SHIM Guard", "shim Guard")


def test_no_prose_file_still_calls_the_product_by_its_old_name() -> None:
    offenders = []
    for relative in _tracked("."):
        if not relative.endswith(".md") or relative.startswith(HISTORY):
            continue
        try:
            text = (ROOT / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        offenders += [f"{relative}: {name}" for name in PROSE_NAMES if name in text]

    assert not offenders, "\n".join(sorted(offenders))


def test_the_release_notes_exist_for_the_declared_version() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    notes = ROOT / "docs" / "releases" / f"{version}.md"

    assert notes.is_file(), f"release.yml expects {notes.relative_to(ROOT)}"
    assert notes.read_text(encoding="utf-8").startswith(f"# shim-cli {version}\n")


def test_the_tested_client_versions_match_the_newest_release_evidence() -> None:
    record = (ROOT / "docs" / "compatibility.md").read_text(encoding="utf-8")
    sections = re.findall(
        r"^## (\d+)\.(\d+)\.(\d+) release evidence\n(.*?)(?=^## |\Z)",
        record,
        flags=re.MULTILINE | re.DOTALL,
    )
    *_, body = max(sections, key=lambda section: tuple(map(int, section[:3])))
    tested = dict(
        re.findall(r"^\| ([^|]+?) \|.*?\btested: (\d+\.\d+\.\d+)", body, re.M)
    )

    assert tested == {
        "Claude Code": TESTED_CLAUDE_VERSION,
        "Codex CLI": TESTED_CODEX_VERSION,
        "GitHub Copilot CLI": TESTED_COPILOT_VERSION,
        # VS Code has no installer and no doctor, so no runtime constant states
        # a tested version; the evidence record is the only place it lives.
        "VS Code": TESTED_VSCODE_VERSION,
    }
