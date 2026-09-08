from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

ARCHIVE_RELATIVE = Path("bin") / "shim.pyz"
PLUGIN_NAMES = ("shim-cli", "shim-guard")
MAX_MANIFEST_BYTES = 1_000_000
_VERSION = re.compile(r'^__version__ = "(?P<version>[0-9][0-9A-Za-z.+-]*)"', re.M)


@dataclass(frozen=True)
class Resolution:
    __slots__ = ("archive_version", "detail", "path_version", "source")

    source: str
    detail: str
    path_version: str | None
    archive_version: str | None

    @property
    def skewed(self) -> bool:
        return (
            self.path_version is not None
            and self.archive_version is not None
            and self.path_version != self.archive_version
        )


def archive_version(archive: Path) -> str | None:
    try:
        with zipfile.ZipFile(archive) as bundle:
            try:
                source = bundle.read("shim_cli/__init__.py").decode("utf-8")
            except KeyError:
                source = bundle.read("shim_guard/__init__.py").decode("utf-8")
    except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile):
        return None
    found = _VERSION.search(source)
    return found.group("version") if found else None


def installed_plugins(home: Path | None = None) -> list[dict]:
    root = Path(home) if home is not None else Path.home()
    manifest = root / ".claude" / "plugins" / "installed_plugins.json"
    try:
        if manifest.stat().st_size > MAX_MANIFEST_BYTES:
            return []
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    plugins = document.get("plugins")
    if not isinstance(plugins, dict):
        return []
    found = []
    for key, entries in plugins.items():
        if key.split("@", 1)[0] not in PLUGIN_NAMES or not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                found.append({"key": key, **entry})
                break
    return found


def resolve(plugin_root: Path | None = None, which=shutil.which) -> Resolution:
    on_path = which("shim-hook") or which("shim-guard-hook")
    root = plugin_root
    if root is None:
        configured = os.environ.get("CLAUDE_PLUGIN_ROOT")
        root = Path(configured) if configured else None
    archive = root / ARCHIVE_RELATIVE if root is not None else None
    bundled = archive if archive is not None and archive.is_file() else None
    bundled_version = archive_version(bundled) if bundled is not None else None

    from shim_cli import __version__

    if on_path is not None:
        return Resolution(
            "path",
            f"The package hook on PATH is active ({on_path}).",
            __version__,
            bundled_version,
        )
    if bundled is not None:
        return Resolution(
            "plugin",
            f"The archive bundled in the plugin is active ({bundled}).",
            None,
            bundled_version,
        )
    return Resolution(
        "none",
        "No hook is runnable; prompts are passing through uninspected.",
        None,
        None,
    )
