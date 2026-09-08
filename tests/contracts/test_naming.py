from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # the 3.10 floor CI also runs
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src/shim_cli", "scripts")
OLD_NAMES = ("SHIM Guard", "shim Guard", "shim_guard")

# The only references that may name the old package: the fallback read of a
# 0.2.0 archive, the old configuration variable, and the module spelling each
# client matcher recognises so `install` replaces a 0.2.0 fragment in place.
_LEGACY_MODULE = 'LEGACY_HOOK_MODULE = "shim_guard.hook"'
ALLOWED = {
    "src/shim_cli/cli/resolution.py": ('bundle.read("shim_guard/__init__.py")',),
    "src/shim_cli/config.py": (
        'os.environ.get(\n            "SHIM_GUARD_CONFIG"\n        )',
    ),
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


def test_the_compatibility_package_holds_only_re_exports() -> None:
    files = sorted(_tracked("src/shim_guard"))

    assert files == ["src/shim_guard/__init__.py", "src/shim_guard/hook.py"]
    for relative in files:
        body = (ROOT / relative).read_text(encoding="utf-8")
        assert "def " not in body
        assert "shim_cli" in body


# R1: the prose allowlist. Each entry is a file whose old-name mentions are the
# migration itself; everything else must be clean.
PROSE_ALLOWED = {
    "docs/releases/0.2.0.md",  # history, unchanged
    "docs/releases/0.3.0.md",
    "docs/compatibility.md",
    "README.md",
    "docs/privacy.md",
    "plugins/shim-cli/README.md",
}
PROSE_NAMES = ("SHIM Guard", "shim Guard")


def test_no_prose_file_still_calls_the_product_by_its_old_name() -> None:
    offenders = []
    for relative in _tracked("."):
        if not relative.endswith(".md") or relative in PROSE_ALLOWED:
            continue
        try:
            text = (ROOT / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        offenders += [f"{relative}: {name}" for name in PROSE_NAMES if name in text]

    assert not offenders, "\n".join(sorted(offenders))


def test_the_allowlisted_prose_only_mentions_the_old_name_as_a_migration() -> None:
    """An allowlist that stops being needed is an allowlist that rots."""
    unused = [
        relative
        for relative in sorted(PROSE_ALLOWED)
        if relative != "docs/releases/0.2.0.md"
        and "shim-guard" not in (ROOT / relative).read_text(encoding="utf-8")
        and "shim_guard" not in (ROOT / relative).read_text(encoding="utf-8")
        and "SHIM_GUARD" not in (ROOT / relative).read_text(encoding="utf-8")
    ]

    assert not unused, f"remove from PROSE_ALLOWED: {unused}"


def test_the_release_notes_exist_for_the_declared_version() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    notes = ROOT / "docs" / "releases" / f"{version}.md"

    assert notes.is_file(), f"release.yml expects {notes.relative_to(ROOT)}"
    assert notes.read_text(encoding="utf-8").startswith(f"# shim-cli {version}\n")
