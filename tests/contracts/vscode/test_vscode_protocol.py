"""The VS Code hook protocol, pinned to captures from a running client.

Recorded 20 September 2026 against VS Code 1.137.0 with Copilot Chat 0.65.0,
through a capture-only hook in `.github/hooks/`. Paths, session ids and the
terminal prompt were scrubbed; the shapes are untouched.

What the run established, and what these tests hold to:

* A prompt can be stopped before it is sent. `continue: false` produced "A hook
  prevented chat from continuing" and the model never answered.
* A tool call can be denied before it runs, and no `PostToolUse` follows.
* A tool result cannot be withheld. `decision: "block"` on a terminal result
  was answered by the model quoting the secret out of it, and `continue: false`
  at the same event did not stop that turn either.
* A `read_file` result never reaches the hook: `tool_response` is `""`. A
  terminal result does arrive in full. So a file read can only be inspected by
  its path, before the read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shim_cli.clients.vscode.tool_events import TOOL_EVENTS
from shim_cli.events.pipeline import REFUSE, REPORT_ONLY, process
from shim_cli.guard import evaluate
from shim_cli.policy import ENFORCE, REPORT

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "probe" / "vscode"


def _raw(name: str) -> bytes:
    return (FIXTURES / f"{name}-1.json").read_bytes()


def _process(name: str, mode: str = ENFORCE):
    event = json.loads(_raw(name))["hook_event_name"]
    return process(TOOL_EVENTS[event], _raw(name), lambda _d, _t: mode, evaluate)


@pytest.mark.parametrize(
    "name",
    (
        "PreToolUse-read_file",
        "PostToolUse-read_file",
        "PreToolUse-run_in_terminal",
        "PostToolUse-run_in_terminal",
    ),
)
def test_every_captured_tool_event_decodes(name: str) -> None:
    event = json.loads(_raw(name))["hook_event_name"]

    decoded = TOOL_EVENTS[event].decode(_raw(name))

    assert decoded.tool == json.loads(_raw(name))["tool_name"]


def test_a_file_read_is_recognised_by_its_camel_case_path() -> None:
    """VS Code spells it `filePath`; Claude Code spells it `file_path`."""
    decoded = TOOL_EVENTS["PreToolUse"].decode(_raw("PreToolUse-read_file"))

    assert decoded.target == "/probe/workspace/plain.txt"
    assert decoded.views_file is True


def test_a_file_read_result_carries_nothing_to_inspect() -> None:
    """Not a gap in shim: VS Code sends `tool_response` empty for read_file.

    The protection for a file read is therefore at `PreToolUse`, on the path,
    before it is read — there is nothing to find afterwards.
    """
    outcome = _process("PostToolUse-read_file")

    assert outcome.output == b""
    assert outcome.record.entities == ()


def test_a_terminal_result_is_inspected_and_reported() -> None:
    outcome = _process("PostToolUse-run_in_terminal")

    document = json.loads(outcome.output)
    assert outcome.record.entities == (("EMAIL", 1), ("SECRET", 1))
    assert document["systemMessage"].startswith("shim: found")
    # Enforce was asked for and is still a report: nothing here can withhold.
    assert outcome.record.action == REPORT


def test_a_clean_terminal_call_passes_without_a_word() -> None:
    """The captured command carries nothing sensitive, so shim stays quiet.

    `cat blocked.txt` is ordinary; the secret is in the file it prints, which
    is why the finding lands on the result rather than the call. Denial of a
    call that does carry data is covered in tests/clients/vscode.
    """
    outcome = _process("PreToolUse-run_in_terminal")

    assert outcome.output == b""
    assert outcome.record.entities == ()


def test_the_powers_match_what_the_client_was_measured_to_grant() -> None:
    assert TOOL_EVENTS["PreToolUse"].power == REFUSE
    assert TOOL_EVENTS["PostToolUse"].power == REPORT_ONLY


def test_the_prompt_capture_is_the_shape_the_hook_parses() -> None:
    from shim_cli.clients.vscode.hook import parse_input

    # The capture is the prompt that proved `continue: false` stops a turn.
    assert parse_input(_raw("UserPromptSubmit-none")) == "say the word pangolin\n"
