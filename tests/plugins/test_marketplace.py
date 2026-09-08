from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLAUDE = ROOT / ".claude-plugin" / "marketplace.json"
CODEX = ROOT / ".agents" / "plugins" / "marketplace.json"

FORBIDDEN = (
    "prevent",
    "guarantee",
    "block sensitive",
    "blocks sensitive",
    "stop secrets",
    "stops secrets",
    "never leaves",
    "100%",
)


def _text(path: Path) -> str:
    return json.dumps(json.loads(path.read_text()), ensure_ascii=False).lower()


@pytest.mark.parametrize("path", (CLAUDE, CODEX), ids=("claude", "codex"))
def test_a_listing_never_promises_protection(path: Path) -> None:
    listing = _text(path)

    found = [word for word in FORBIDDEN if word in listing]

    assert not found, (
        f"{path.name} claims {found}. shim detects and reports; with the "
        "shipped policy it does not stop a typed secret from reaching the "
        "model. See the warning at the top of README.md."
    )


@pytest.mark.parametrize("path", (CLAUDE, CODEX), ids=("claude", "codex"))
def test_a_listing_points_at_a_plugin_that_exists(path: Path) -> None:
    document = json.loads(path.read_text())

    for plugin in document["plugins"]:
        source = plugin["source"]
        relative = source if isinstance(source, str) else source["path"]
        assert (ROOT / relative).is_dir(), relative
        assert (ROOT / relative / "hooks").is_dir()


def test_the_claude_listing_claims_masking_because_claude_can_mask() -> None:
    description = json.loads(CLAUDE.read_text())["plugins"][0]["description"].lower()

    assert "mask" in description, "Claude Code masks tool results — say so"
    assert "tool results" in description
    assert "no network" in description or "locally" in description


def test_the_codex_listing_does_not_claim_masking_because_codex_cannot() -> None:
    description = json.loads(CODEX.read_text())["plugins"][0]["description"].lower()

    assert "mask" not in description
    assert "redact" not in description
    assert "told" in description or "report" in description


@pytest.mark.parametrize("path", (CLAUDE, CODEX), ids=("claude", "codex"))
def test_a_listing_leads_with_what_the_user_gets(path: Path) -> None:
    description = json.loads(path.read_text())["plugins"][0]["description"]

    assert len(description) <= 300, "stores truncate; keep it readable"
    assert description[0].isupper() and description.rstrip().endswith(".")


def test_both_listings_name_the_same_plugin() -> None:
    claude = {plugin["name"] for plugin in json.loads(CLAUDE.read_text())["plugins"]}
    codex = {plugin["name"] for plugin in json.loads(CODEX.read_text())["plugins"]}

    assert claude == codex == {"shim-cli", "shim-guard"}


ALIAS_DESCRIPTION = (
    "Former name of shim-cli. Existing installs keep updating; "
    "new installs should use shim-cli."
)


@pytest.mark.parametrize("path", (CLAUDE, CODEX), ids=("claude", "codex"))
def test_a_listing_is_named_for_the_product(path: Path) -> None:
    assert json.loads(path.read_text())["name"] == "shim-cli"


@pytest.mark.parametrize("path", (CLAUDE, CODEX), ids=("claude", "codex"))
def test_the_alias_entry_keeps_a_020_install_resolvable(path: Path) -> None:
    plugins = json.loads(path.read_text())["plugins"]
    names = [plugin["name"] for plugin in plugins]

    assert names == ["shim-cli", "shim-guard"]
    alias, current = plugins[1], plugins[0]
    assert alias["description"] == ALIAS_DESCRIPTION
    assert alias["source"] == current["source"], "the alias must not fork the source"
    assert alias["category"] == current["category"]


def test_both_listings_point_at_the_same_directory() -> None:
    def directory(path: Path) -> set[str]:
        return {
            plugin["source"]
            if isinstance(plugin["source"], str)
            else plugin["source"]["path"]
            for plugin in json.loads(path.read_text())["plugins"]
        }

    assert directory(CLAUDE) == directory(CODEX) == {"./plugins/shim-cli"}


README = ROOT / "README.md"


def test_the_readme_install_commands_name_the_manifest_plugin() -> None:
    """A reader pastes these; they must not drift from the manifests."""
    current = json.loads(CLAUDE.read_text())["plugins"][0]["name"]
    marketplace = json.loads(CLAUDE.read_text())["name"]
    text = README.read_text(encoding="utf-8")

    assert f"/plugin install {current}@{marketplace}" in text
    assert f"codex plugin add {current}@{marketplace}" in text
    assert "/plugin marketplace add GetSHIM/shim-cli" in text
    assert "codex plugin marketplace add GetSHIM/shim-cli" in text
