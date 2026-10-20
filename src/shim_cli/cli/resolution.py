from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from shim_cli.clients.claude import settings as claude_settings
from shim_cli.clients.claude.settings import PLUGIN_NAMES

ARCHIVE_RELATIVE = Path("bin") / "shim.pyz"
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
            source = bundle.read("shim_cli/__init__.py").decode("utf-8")
    except (OSError, KeyError, UnicodeDecodeError, zipfile.BadZipFile):
        return None
    found = _VERSION.search(source)
    return found.group("version") if found else None


def _switched_off(settings: Path) -> set[str]:
    """Plugins the user's settings file turns off; they run no hook."""
    try:
        if settings.stat().st_size > MAX_MANIFEST_BYTES:
            return set()
        enabled = json.loads(settings.read_text(encoding="utf-8")).get("enabledPlugins")
    except (OSError, ValueError, UnicodeDecodeError, AttributeError):
        return set()
    if not isinstance(enabled, dict):
        return set()
    return {key for key, value in enabled.items() if value is False}


def installed_plugins(home: Path | None = None) -> list[dict]:
    # Where Claude Code keeps plugins follows CLAUDE_CONFIG_DIR, as install does.
    try:
        folder = claude_settings.target_path(home).parent
    except ValueError:
        return []
    manifest = folder / "plugins" / "installed_plugins.json"
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
    off = _switched_off(folder / "settings.json")
    for key, entries in plugins.items():
        if key.split("@", 1)[0] not in PLUGIN_NAMES or not isinstance(entries, list):
            continue
        if key in off:
            continue
        for entry in entries:
            if isinstance(entry, dict):
                found.append({"key": key, **entry})
                break
    return found


def resolve(plugin_root: Path | None = None, which=shutil.which) -> Resolution:
    on_path = which("shim-hook")
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
        "No hook is runnable from this installation; unless the plugin runs its own "
        "launcher, prompts pass through uninspected.",
        None,
        None,
    )
