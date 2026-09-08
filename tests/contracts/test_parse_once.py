import json
from contextlib import nullcontext

import pytest

from shim_cli import hook
from shim_cli.clients import user_prompt_hook


@pytest.mark.parametrize(
    "client,event,fields",
    [
        ("codex", "UserPromptSubmit", {"prompt": "safe"}),
        ("claude", "UserPromptSubmit", {"prompt": "safe"}),
        (
            "copilot",
            "userPromptTransformed",
            {"prompt": "safe", "transformedPrompt": "safe"},
        ),
        (
            "claude",
            "PostToolUse",
            {"tool_name": "Read", "tool_response": {"text": "safe"}},
        ),
    ],
)
def test_successful_hook_parses_json_once(monkeypatch, client, event, fields):
    calls = 0
    parse = user_prompt_hook.parse_object

    def counted(raw):
        nonlocal calls
        calls += 1
        return parse(raw)

    monkeypatch.setattr(user_prompt_hook, "parse_object", counted)
    monkeypatch.setattr(hook, "_silence_dependencies", nullcontext)
    assert (
        hook._output(json.dumps(dict(hook_event_name=event, **fields)).encode(), client)
        == b""
    )
    assert calls == 1
