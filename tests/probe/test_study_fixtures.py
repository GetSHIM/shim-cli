"""The study document may not quote a number the code no longer produces.

`docs/study-2026-09-08-image-repeat-cache.md` reports what five scripted Claude
Code sessions showed. Two kinds of claim live in it, and they are checked
differently:

  - what the client did (request counts, cache series) cannot be recomputed
    without spending money, so it is pinned to a captured fixture;
  - what *shim* did to a tool result can be recomputed exactly, so it is, from
    the same generator the harness used.

The second kind is the one that mattered: the first run of this study compared
the diet against itself, because the client truncated every tool result and the
diet correctly declines to rewrite a document that no longer parses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from shim_cli.events.diet import DEFAULT_TRANSFORMS, shrink

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "probe" / "study-2026-09-08.json"
)


def _document() -> dict:
    if not FIXTURE.is_file():
        pytest.skip("study fixture not captured")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _harness_json(records: int) -> str:
    """The same bytes `scripts/probe/study.py` wrote into the workspace."""
    scripts = Path(__file__).resolve().parents[2] / "scripts" / "probe"
    sys.path.insert(0, str(scripts))
    try:
        from study import _record  # ty: ignore[unresolved-import]
    finally:
        sys.path.remove(str(scripts))
    document = {
        "dataset": "data-1",
        "records": [_record(1000 + index) for index in range(records)],
    }
    return json.dumps(document, indent=2) + "\n"


def test_the_quoted_compaction_is_what_the_diet_still_does() -> None:
    diet = _document()["diet"]
    text = _harness_json(diet["records_per_file"])
    compacted, applied = shrink(text, DEFAULT_TRANSFORMS)

    assert len(text) == diet["file_bytes"]
    assert len(compacted) == diet["compacted_bytes"]
    assert list(applied) == diet["transforms"]


def test_a_truncated_result_is_still_left_alone() -> None:
    """The finding that invalidated the study's first run."""
    diet = _document()["diet"]
    whole = _harness_json(diet["truncated_source_records"])
    cut = whole[: diet["client_truncates_at_bytes"]]

    compacted, applied = shrink(cut, DEFAULT_TRANSFORMS)

    assert applied == ()
    assert compacted == cut


def test_every_measured_session_kept_its_cache_prefix() -> None:
    sessions = _document()["sessions"]

    assert sessions
    for session in sessions:
        assert session["prefix_preserved"], session["label"]
        assert session["reads_never_fall"], session["label"]


def test_the_mid_session_flip_is_present_and_did_not_break_the_prefix() -> None:
    """R1's third session: the one configuration change that could have."""
    sessions = _document()["sessions"]
    flipped = [item for item in sessions if item["label"].startswith("flip")]

    assert flipped, "the flip session is the point of R1"
    for session in flipped:
        assert session["prefix_preserved"]


def test_the_document_quotes_the_fixture() -> None:
    root = Path(__file__).resolve().parents[2]
    document = root / "docs" / "study-2026-09-08-image-repeat-cache.md"
    if not document.is_file():
        pytest.skip("study document not written")
    text = document.read_text(encoding="utf-8")
    diet = _document()["diet"]

    assert f"{diet['file_bytes']:,}" in text
    assert f"{diet['compacted_bytes']:,}" in text
    assert "PENDING" not in text
