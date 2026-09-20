"""A Claude Code install also runs inside VS Code, and must behave there.

VS Code reads `~/.claude/settings.json` hooks by default, so `shim install
claude` registers shim in two clients at once. Before this, the prompt block
went out in Claude's shape, `decision: "block"`, which VS Code ignores: a user
who asked for enforcement had their prompt sent anyway and was told it was
blocked. That is the one failure mode worse than not running at all.

The clients are told apart by their payloads, not their environment. The
environment is no help: the VS Code capture carried CLAUDE_CODE_* variables
because the editor inherited them from the terminal that launched it.
"""

from __future__ import annotations

import json

import pytest

from shim_cli.hook import _is_vscode_payload, _output

VSCODE_PROMPT = {
    "timestamp": "2026-09-20T08:41:10.545Z",
    "hook_event_name": "UserPromptSubmit",
    "session_id": "v1",
    "transcript_path": "/probe/workspace/.vscode/transcripts/probe.jsonl",
    "prompt": "mail me at ayse.yilmaz@example.com",
    "cwd": "/probe/workspace",
}
CLAUDE_PROMPT = {
    "hook_event_name": "UserPromptSubmit",
    "session_id": "c1",
    "transcript_path": "/probe/.claude/projects/-probe-workspace/c1.jsonl",
    "cwd": "/probe/workspace",
    "prompt_id": "00000000-0000-4000-8000-000000000033",
    "permission_mode": "default",
    "prompt": "mail me at ayse.yilmaz@example.com",
}


def _enforce(root) -> None:
    directory = root / "config" / "shim"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(
        '[mode]\nuser-prompt = "enforce"\n', encoding="utf-8"
    )


def test_a_vs_code_event_is_recognised_by_what_both_clients_always_send() -> None:
    assert _is_vscode_payload(VSCODE_PROMPT) is True
    assert _is_vscode_payload(CLAUDE_PROMPT) is False


def test_the_claude_hook_stops_a_vs_code_prompt_in_the_shape_vs_code_obeys(
    _isolated_roots,
) -> None:
    _enforce(_isolated_roots)

    document = json.loads(_output(json.dumps(VSCODE_PROMPT).encode(), "claude"))

    # `decision: "block"` is ignored by VS Code; `continue: false` is not.
    assert document["continue"] is False
    assert document["stopReason"].startswith("shim blocked this prompt: EMAIL (1).")
    assert "decision" not in document


def test_a_real_claude_prompt_still_gets_claude_s_own_block(_isolated_roots) -> None:
    _enforce(_isolated_roots)

    document = json.loads(_output(json.dumps(CLAUDE_PROMPT).encode(), "claude"))

    assert document["decision"] == "block"
    assert document["suppressOriginalPrompt"] is True


def test_a_vs_code_tool_result_is_not_masked_through_the_claude_hook(
    _isolated_roots,
) -> None:
    """Claude's mask would emit `updatedToolOutput`, which VS Code ignores.

    Reporting it as masked would tell the user a value was replaced when the
    model read the original.
    """
    result = {
        "timestamp": "2026-09-20T08:44:18.224Z",
        "hook_event_name": "PostToolUse",
        "session_id": "v1",
        "tool_name": "run_in_terminal",
        "tool_input": {"command": "cat secrets.env"},
        "tool_response": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        "cwd": "/probe/workspace",
    }

    document = json.loads(_output(json.dumps(result).encode(), "claude"))

    assert "updatedToolOutput" not in json.dumps(document)
    assert document["systemMessage"] == (
        "shim: found SECRET (1) in run_in_terminal. Not modified."
    )


@pytest.mark.parametrize("client", ("codex", "copilot", "vscode"))
def test_no_other_client_is_re_routed(client: str, _isolated_roots) -> None:
    """Only the Claude path is ambiguous.

    Copilot CLI sends `timestamp` and no `permission_mode` too, so re-routing
    on that alone would break the client it is installed for.
    """
    from shim_cli import hook

    calls = []
    original = hook._is_vscode_payload
    hook._is_vscode_payload = lambda document: (
        calls.append(document) or original(document)
    )
    try:
        _output(json.dumps(VSCODE_PROMPT).encode(), client)
    finally:
        hook._is_vscode_payload = original

    assert calls == []
