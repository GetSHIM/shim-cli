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


RELEASE = WORKFLOWS / "release.yml"


def _step(marker: str) -> str:
    text = RELEASE.read_text(encoding="utf-8")
    start = text.rindex("\n      - ", 0, text.index(marker))
    end = text.find("\n      - ", start + 1)
    return text[start:end]


def test_the_sbom_scans_the_installed_wheel_and_is_checked() -> None:
    text = RELEASE.read_text(encoding="utf-8")
    sbom = _step("uses: anchore/sbom-action@")
    check = _step("name: Require the SBOM to list")

    assert "path: /tmp/shim-cli-benchmark" in sbom
    assert "dist/packages" not in sbom
    assert text.index('--no-deps "dist/packages/shim-') < text.index(sbom)
    assert text.index(sbom) + len(sbom) == text.index(check)
    assert '["components"]' in check and "dist/requirements.lock" in check


def test_the_attestation_bundles_are_hashed_and_attached() -> None:
    bundles = (
        '"dist/shim-${GITHUB_REF_NAME#v}.intoto.jsonl"',
        '"dist/shim-${GITHUB_REF_NAME#v}-py3-none-any.whl.sigstore.json"',
        "dist/shim.pyz.sigstore.json",
    )
    hashed = _step("name: Attach and hash the attestation bundles")
    flat = _step("name: Verify checksums from a flat download directory")
    create = _step("gh release create")

    assert all(bundle in flat and bundle in create for bundle in bundles)
    assert ">> dist/SHA256SUMS" in hashed
    assert all(
        name in hashed
        for name in (".intoto.jsonl", ".whl.sigstore.json", " shim.pyz.sigstore.json")
    )
    attest = [_step("id: provenance"), _step("id: sbom")]
    assert all("dist/packages/*" in step and "dist/shim.pyz" in step for step in attest)


def test_the_release_verifies_attestations_before_it_publishes() -> None:
    text = RELEASE.read_text(encoding="utf-8")
    verify = _step("gh attestation verify")

    assert text.index(verify) < text.index("gh release create")
    assert (
        "attestations: read"
        in text[text.index("\n  release:") : text.index("\n  publish:")]
    )
    assert verify.count("gh attestation verify ") == 3
    for subject in (
        '"dist/packages/shim-${GITHUB_REF_NAME#v}-py3-none-any.whl"',
        '"dist/packages/shim-${GITHUB_REF_NAME#v}.tar.gz"',
        "dist/shim.pyz",
    ):
        assert f'gh attestation verify {subject} --repo "$GITHUB_REPOSITORY"' in verify
