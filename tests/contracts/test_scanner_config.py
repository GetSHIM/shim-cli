from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # the 3.10 floor CI also runs
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]


def test_the_scanner_ignores_only_named_fixture_files() -> None:
    ignored = tomllib.loads((ROOT / ".plugin-scanner.toml").read_text())["scanner"][
        "ignore_paths"
    ]

    assert ignored
    for path in ignored:
        assert not set("*?[") & set(path), path
        assert path.startswith(("tests/", "scripts/", ".github/")), path
        assert (ROOT / path).is_file(), path
