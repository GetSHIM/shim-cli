from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "probe" / "claude"
PATH_KEYS = ("cwd", "transcript_path", "file_path", "filePath")
ALLOWED_PREFIXES = ("/probe", "/home/probe", "/usr/bin")
FORBIDDEN = ("/Users/", "mertcansaglam", ".vscode", "claude-501", "/private/tmp")


def _fixtures() -> list[Path]:
    return sorted(FIXTURES.glob("*.json"))


def _load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _paths(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in PATH_KEYS and isinstance(item, str):
                found.append(item)
            found.extend(_paths(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_paths(item))
    return found


def test_the_probe_captured_enough_evidence() -> None:
    fixtures = _fixtures()
    assert len(fixtures) >= 8

    events, tools = set(), set()
    for path in fixtures:
        payload = _load(path.name)
        events.add(payload["hook_event_name"])
        if isinstance(payload.get("tool_name"), str):
            tools.add(payload["tool_name"])

    assert {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PostToolBatch",
    } <= events
    assert len(tools) >= 4
    assert {"Read", "Bash", "Grep", "WebFetch", "Write"} <= tools
    assert any(name.startswith("mcp__") for name in tools)


@pytest.mark.parametrize("fixture", _fixtures(), ids=lambda path: path.name)
def test_fixtures_carry_no_machine_identity(fixture: Path) -> None:
    text = fixture.read_text(encoding="utf-8")
    for marker in FORBIDDEN:
        assert marker not in text, f"{fixture.name} leaks {marker!r}"
    for path in _paths(json.loads(text)):
        if path.startswith("/"):
            assert path.startswith(ALLOWED_PREFIXES), f"{fixture.name}: {path}"


def test_post_tool_use_carries_the_whole_read_result_the_model_saw() -> None:
    payload = _load("PostToolUse-Read-read-large-1.json")
    result = payload["tool_response"]
    assert isinstance(result, dict)
    file = result["file"]
    assert isinstance(file, dict)

    content = file["content"]
    assert isinstance(content, str)
    assert len(content.encode()) > 50_000
    assert file["truncatedByTokenCap"] is True
    assert file["numLines"] < file["totalLines"]
    assert content.startswith("Contact alice@example.com for access.")


def test_read_results_use_two_different_shapes_across_events() -> None:
    single = _load("PostToolUse-Read-batch-1.json")["tool_response"]
    batch = _load("PostToolBatch-none-batch-1.json")["tool_calls"]

    assert isinstance(single, dict)
    assert set(single) == {"type", "file"}
    assert isinstance(batch, list)
    assert all(isinstance(call["tool_response"], str) for call in batch)
    assert batch[0]["tool_response"].startswith("1\t")


def test_web_fetch_results_never_reach_the_hook_as_html() -> None:
    result = _load("PostToolUse-WebFetch-webfetch-1.json")["tool_response"]
    assert isinstance(result, dict)
    assert set(result) == {"bytes", "code", "codeText", "result", "durationMs", "url"}
    assert isinstance(result["result"], str)
    assert "<html" not in result["result"].lower()
    assert result["bytes"] > len(result["result"].encode()) // 4


def test_local_write_payloads_expose_their_content_before_the_write() -> None:
    payload = _load("PreToolUse-Write-write-1.json")
    tool_input = payload["tool_input"]
    assert isinstance(tool_input, dict)
    assert tool_input["content"] == "password = SuperSecret123!"


def test_mcp_arguments_and_results_are_structured_not_flat_text() -> None:
    before = _load("PreToolUse-mcp__probe__probe_echo-mcp-echo-1.json")
    after = _load("PostToolUse-mcp__probe__probe_echo-mcp-echo-1.json")

    assert before["tool_name"].startswith("mcp__")
    assert before["tool_input"] == {
        "customer_email": "alice@example.com",
        "note": "ping",
    }
    result = after["tool_response"]
    assert isinstance(result, list)
    assert result[0]["type"] == "text"


def test_bash_input_is_free_form_command_text() -> None:
    tool_input = _load("PreToolUse-Bash-bash-connection-string-1.json")["tool_input"]
    assert isinstance(tool_input, dict)
    assert "postgresql://" in tool_input["command"]


def test_failed_tools_report_an_error_string_and_no_result() -> None:
    payload = _load("PostToolUseFailure-Bash-bash-failure-1.json")
    assert "tool_response" not in payload
    assert isinstance(payload["error"], str)
    assert payload["is_interrupt"] is False


def test_the_stop_event_carries_only_the_turn_s_last_text_block() -> None:
    """The captured turn said CHECKING, called Read, then answered."""
    message = _load("Stop-none-model-reply-1.json")["last_assistant_message"]

    assert message == "The file sets OWNER_EMAIL to alice@example.com."
    assert "CHECKING" not in message


def test_the_detector_counts_what_the_model_wrote_in_that_capture() -> None:
    from shim_cli.guard import evaluate

    message = _load("Stop-none-model-reply-1.json")["last_assistant_message"]

    assert dict(evaluate(message).counts) == {"EMAIL": 1}


def test_a_pdf_read_is_a_file_view_holding_the_document_as_base64() -> None:
    payload = _load("PostToolUse-Read-read-pdf-1.json")
    result = payload["tool_response"]

    assert payload["tool_input"]["file_path"].endswith("/doc.pdf")
    assert result["type"] == "pdf"
    assert set(result["file"]) == {"filePath", "base64", "originalSize"}
    assert base64.b64decode(result["file"]["base64"]).startswith(b"%PDF-")
    assert "probe document" not in json.dumps(result), "no extracted text"


def test_a_word_read_is_refused_before_any_tool_event() -> None:
    (call,) = _load("PostToolBatch-none-read-docx-1.json")["tool_calls"]

    assert call["tool_input"]["file_path"].endswith("/doc.docx")
    assert call["tool_response"].startswith("<tool_use_error>This tool cannot read")
    assert not list(FIXTURES.glob("*Use*-read-docx-*.json"))


def test_a_page_fetched_with_curl_reaches_the_hook_as_markup_up_to_30000() -> None:
    result = _load("PostToolUse-Bash-bash-curl-html-1.json")["tool_response"]

    assert result["stdout"].startswith("<!doctype html>")
    assert len(result["stdout"]) == 30_000
    assert result["persistedOutputSize"] > 30_000


def test_a_page_from_an_mcp_server_reaches_the_hook_whole() -> None:
    (block,) = _load("PostToolUse-mcp__probe__probe_echo-mcp-html-1.json")[
        "tool_response"
    ]

    assert block["type"] == "text"
    assert block["text"].startswith("<!doctype html>")
    assert block["text"].endswith("</body></html>\n")


@pytest.mark.parametrize(
    "name,converted",
    [
        # Command output may be a file the model edits next: never converted.
        ("PostToolUse-Bash-bash-curl-html-1.json", False),
        ("PostToolUse-mcp__probe__probe_echo-mcp-html-1.json", True),
        ("PostToolUse-Read-read-pdf-1.json", False),
    ],
)
def test_the_html_diet_converts_mcp_pages_and_never_a_command_or_file(
    name: str, converted: bool
) -> None:
    from shim_cli.clients.claude.tool_events import TOOL_EVENTS
    from shim_cli.events.pipeline import process
    from shim_cli.guard import evaluate

    outcome = process(
        TOOL_EVENTS["PostToolUse"],
        (FIXTURES / name).read_bytes(),
        lambda direction, tool: "enforce",
        evaluate,
        ("json", "html"),
    )

    assert (outcome.record.transforms == ("html",)) is converted
    assert (b"converted from HTML" in outcome.output) is converted
