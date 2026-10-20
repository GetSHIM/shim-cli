"""The model is told the source holds no placeholders only when that is true."""

from __future__ import annotations

import json

from shim_cli.clients.claude.tool_events import post_tool_use
from shim_cli.policy import MASK

MASKED = "shim: masked EMAIL (1) in Read."


def _context(*literal: bool) -> str:
    output = post_tool_use(MASK, {}, MASKED, "EMAIL (1)", "Read", "Read", *literal)
    return json.loads(output)["hookSpecificOutput"]["additionalContext"]


def test_a_source_without_placeholder_shaped_text_is_said_to_have_none() -> None:
    assert (
        _context()
        == _context(False)
        == (
            f"{MASKED} Placeholders such as <EMAIL_1> stand for real values in the "
            "source; the source does not contain placeholders."
        )
    )


def test_a_source_with_placeholder_shaped_text_is_not_said_to_have_none() -> None:
    assert _context(True) == (
        f"{MASKED} Placeholders such as <EMAIL_1> stand for real values in the source."
    )
