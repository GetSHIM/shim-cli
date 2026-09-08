from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src/shim_cli", "scripts")
OLD_NAMES = ("SHIM Guard", "shim Guard", "shim_guard")

# The only references that may name the old package: the alias console script,
# the fallback read of a 0.2.0 archive, and the old configuration variable.
ALLOWED = {
    "src/shim_cli/cli/resolution.py": ('bundle.read("shim_guard/__init__.py")',),
    "src/shim_cli/config.py": (
        'os.environ.get(\n            "SHIM_GUARD_CONFIG"\n        )',
    ),
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
