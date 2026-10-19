"""The payload walk reads every place a value can sit in a JSON event: object
keys, numbers, and values whose meaning is in their key. Binary bodies are
never rewritten into something that is no longer the file."""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

import pytest

from shim_cli.clients.claude.tool_events import TOOL_EVENTS, WITHHELD
from shim_cli.clients.vscode.tool_events import TOOL_EVENTS as VSCODE_EVENTS
from shim_cli.events.payload import KEY_CONTEXT, inspect, mask
from shim_cli.events.pipeline import process
from shim_cli.guard import evaluate
from shim_cli.policy import ENFORCE, OBSERVE

JANE = "jane.doe@example.com"
JOHN = "john.roe@example.com"
CARD = 4111111111111111
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
PROBE = Path(__file__).parents[1] / "fixtures" / "probe" / "claude"


def _counts(findings) -> dict:
    totals: dict = {}
    for _path, decision in findings:
        for entity, count in decision.counts:
            totals[entity] = totals.get(entity, 0) + count
    return totals


def _event(name: str, tool: str, body: object) -> bytes:
    root = "tool_input" if name == "PreToolUse" else "tool_response"
    return json.dumps(
        {
            "hook_event_name": name,
            "session_id": "s",
            "tool_name": tool,
            "tool_input": body if root == "tool_input" else {"file_path": "/w/f"},
            "tool_response": body if root == "tool_response" else None,
        }
    ).encode()


def _process(name, tool, body, mode=ENFORCE, check=evaluate, events=TOOL_EVENTS):
    return process(events[name], _event(name, tool, body), lambda *_: mode, check)


def _counting(seen: list):
    def counting(text: str, **kwargs):
        seen.append(text)
        return evaluate(text, **kwargs)

    return counting


def _pdf(text: str) -> str:
    """A minimal uncompressed PDF whose one content stream shows `text`."""
    stream = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET"
    document = (
        "%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        "2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        "3 0 obj << /Type /Page /Parent 2 0 R /Contents 4 0 R >> endobj\n"
        f"4 0 obj << /Length {len(stream)} >> stream\n{stream}\nendstream endobj\n"
        "trailer << /Root 1 0 R >>\n%%EOF\n"
    )
    return base64.b64encode(document.encode()).decode()


# Keys --------------------------------------------------------------------


def test_an_address_used_as_a_key_is_masked_and_counted() -> None:
    result, findings, changed = mask({"recipients": {JANE: "to"}}, evaluate)

    assert changed is True
    assert result == {"recipients": {"<EMAIL_1>": "to"}}
    assert _counts(findings) == {"EMAIL": 1}


def test_two_keys_with_two_addresses_get_two_placeholders() -> None:
    result, _findings, _changed = mask({JANE: "to", JOHN: "cc"}, evaluate)

    assert result == {"<EMAIL_1>": "to", "<EMAIL_2>": "cc"}


def test_a_key_keeps_the_number_its_value_has_elsewhere_in_the_event() -> None:
    result, _findings, _changed = mask(
        {"owner": JANE, "by_email": {JANE: {"name": "Jane"}}}, evaluate
    )

    assert result == {"owner": "<EMAIL_1>", "by_email": {"<EMAIL_1>": {"name": "Jane"}}}


def test_keys_that_would_be_masked_to_one_name_cannot_be_masked() -> None:
    # One card number written two ways is one value with one placeholder, so
    # the two keys cannot both be kept: no masked version exists.
    result = inspect({"4111 1111 1111 1111": "a", "4111111111111111": "b"}, evaluate)

    assert (result.status, result.unmaskable) == ("complete", True)
    assert (result.value, result.changed) == (None, False)
    assert _counts(result.findings) == {"CREDIT_CARD": 2}


def test_an_mcp_call_with_colliding_keys_is_refused() -> None:
    outcome = _process(
        "PreToolUse",
        "mcp__crm__bulk_tag",
        {"tags": {"4111 1111 1111 1111": "vip", "4111111111111111": "key"}},
    )

    output = json.loads(outcome.output)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert b"4111" not in outcome.output
    assert outcome.record.entities == (("CREDIT_CARD", 2),)


def test_a_result_keyed_by_address_reaches_the_model_masked() -> None:
    outcome = _process(
        "PostToolUse", "mcp__db__query", {"by_email": {JANE: {"card": CARD}}}
    )

    output = json.loads(outcome.output)["hookSpecificOutput"]
    assert output["updatedToolOutput"] == {
        "by_email": {"<EMAIL_1>": {"card": "<CREDIT_CARD_1>"}}
    }
    assert outcome.record.entities == (("CREDIT_CARD", 1), ("EMAIL", 1))


def test_the_diet_reshapes_text_but_never_a_key() -> None:
    spaced = json.dumps({"rows": list(range(30))}, indent=2)
    result = inspect({"results": {spaced: spaced}}, evaluate, ("json",))

    assert result.transforms == ("json",)
    compact = json.dumps({"rows": list(range(30))}, separators=(",", ":"))
    assert result.value == {"results": {spaced: compact}}


# Numbers -----------------------------------------------------------------


def test_a_card_sent_as_a_number_is_masked_and_counted() -> None:
    result, findings, changed = mask({"card": CARD}, evaluate)

    assert changed is True
    assert result == {"card": "<CREDIT_CARD_1>"}
    assert _counts(findings) == {"CREDIT_CARD": 1}


def test_numbers_in_an_mcp_call_are_counted_and_refused() -> None:
    outcome = _process(
        "PreToolUse",
        "mcp__billing__create_customer",
        {
            "card_number": CARD,
            "phone": 905321234567,
            "tckn": 10000000146,
            "note": f"card {CARD}",
        },
    )

    output = json.loads(outcome.output)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert outcome.record.entities == (
        ("CREDIT_CARD", 2),
        ("PHONE", 1),
        ("TR_NATIONAL_ID", 1),
    )


def test_numbers_and_flags_without_a_value_keep_their_type() -> None:
    document = {"count": 2, "ratio": 0.5, "ok": True, "none": None, "card": CARD}

    result, _findings, _changed = mask(document, evaluate)

    assert result == {
        "count": 2,
        "ratio": 0.5,
        "ok": True,
        "none": None,
        "card": "<CREDIT_CARD_1>",
    }
    assert type(result["count"]) is int and type(result["ratio"]) is float


# Key context ---------------------------------------------------------------


def test_a_value_under_a_secret_key_is_masked() -> None:
    result, findings, _changed = mask({"password": "hunter2-xyz9"}, evaluate)

    assert result == {"password": "<SECRET_1>"}
    assert _counts(findings) == {"SECRET": 1}


def test_a_value_named_by_its_sibling_is_masked() -> None:
    document = [{"name": "DB_PASSWORD", "value": "hunter2-xyz9"}]

    result, _findings, _changed = mask(document, evaluate)

    assert result == [{"name": "DB_PASSWORD", "value": "<SECRET_1>"}]


def test_a_phone_number_under_a_phone_key_is_masked() -> None:
    result, _findings, _changed = mask({"contact": {"phone": 2125550123}}, evaluate)

    assert result == {"contact": {"phone": "<PHONE_1>"}}


def test_a_value_under_a_plain_key_is_read_as_it_is_alone() -> None:
    # Key context only adds: a value still has every finding it has alone,
    # including a base64 block cut at its very start.
    cut = "x" + base64.b64encode(b"export DB_PASSWORD=hunter2abcdef\n").decode()
    alone = evaluate(cut)
    assert alone.findings, "the detector reads the cut block when it starts the text"

    result, _findings, _changed = mask({"stdout": cut}, evaluate)

    assert result == {"stdout": alone.redacted_text}


def test_a_value_is_masked_whole_when_its_key_runs_into_it() -> None:
    # The finding in context runs past the value's closing quote; the value
    # is still masked to its end.
    result, _findings, _changed = mask(
        {"cmd": "mysql --password=SuperSecret123!"}, evaluate
    )

    assert result == {"cmd": "mysql --password=<SECRET_1>"}


def test_the_mcp_request_from_the_report_is_refused_whole() -> None:
    outcome = _process(
        "PreToolUse",
        "mcp__http__request",
        {
            "url": "https://api.example.com/v1/sync",
            "headers": {"X-Api-Key": "0123456789abcdef01234567"},
            "body": {"db_password": "example-password-0123"},
            "cc": {JANE: "to"},
        },
    )

    output = json.loads(outcome.output)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert outcome.record.entities == (("EMAIL", 1), ("SECRET", 2))


def test_a_whole_number_written_as_a_float_is_masked_whole() -> None:
    # JSON 9.05321234567e11 is the number 905321234567: never "<PHONE_1>.0".
    result, findings, _changed = mask(
        {"phone": 9.05321234567e11, "card": 4.111111111111111e15}, evaluate
    )

    assert result == {"phone": "<PHONE_1>", "card": "<CREDIT_CARD_1>"}
    assert _counts(findings) == {"CREDIT_CARD": 1, "PHONE": 1}


# What key context costs -----------------------------------------------------


def test_a_long_value_is_read_with_its_key_only_at_its_start() -> None:
    value = f"mail {JANE} " + "plain words " * 2_000
    seen: list = []

    result, _findings, _changed = mask({"content": value}, _counting(seen))

    assert result == {"content": value.replace(JANE, "<EMAIL_1>")}
    framed = [text for text in seen if text.startswith('"content": "')]
    assert framed == [f'"content": "{value[:KEY_CONTEXT]}"']


def test_a_finding_its_key_starts_is_read_to_the_end_of_a_long_value() -> None:
    # Only the key makes this a secret, and it runs past the bounded start:
    # the value is read with its key in full, and masked whole.
    value = "Ab1" * 2_000
    assert not evaluate(value).findings

    result, findings, _changed = mask({"password": value}, evaluate)

    assert result == {"password": "<SECRET_1>"}
    assert _counts(findings) == {"SECRET": 1}


def test_a_finding_that_reaches_the_cut_is_read_with_its_key_in_full() -> None:
    # A detector for which the key carries a finding that ends exactly where
    # the bounded start is cut further than the value alone carries it.
    from shim_cli.guard.models import Finding, GuardDecision

    text = "a" * (KEY_CONTEXT + 1_000)
    prefix = '"k": "'
    ends = {text: KEY_CONTEXT, f'{prefix}{text}"': KEY_CONTEXT + 500}

    def stub(seen: str, **_kwargs) -> GuardDecision:
        if len(seen) < KEY_CONTEXT:  # the key itself
            return GuardDecision((), seen, False, 0)
        offset = len(prefix) if seen.startswith(prefix) else 0
        end = offset + ends.get(seen, KEY_CONTEXT)
        start = offset + KEY_CONTEXT - 100
        found = (Finding("SECRET", start, end, 0.9, ""),)
        return GuardDecision(found, f"{seen[:start]}<SECRET_1>{seen[end:]}", False, 0)

    result, _findings, _changed = mask({"k": text}, stub)

    assert result["k"] == f"{text[: KEY_CONTEXT - 100]}<SECRET_1>{text[-500:]}"


def test_a_text_repeated_in_one_event_is_read_once() -> None:
    seen: list = []

    mask([{"status": "open", "owner": JANE}] * 50, _counting(seen))

    assert seen.count("open") == 1
    assert seen.count(f'"owner": "{JANE}"') == 1
    assert seen.count("status") == 1


# Binary bodies -------------------------------------------------------------

TEXT = f"Contact {JANE} key {AWS_KEY} card {CARD}"
TEXT_COUNTS = (("CREDIT_CARD", 1), ("EMAIL", 1), ("SECRET", 1))


def _png() -> str:
    """A real 1x1 PNG, as base64."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data))
        return struct.pack(">I", len(data)) + kind + data + crc

    raw = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(raw).decode()


def test_the_probe_pdf_read_passes_untouched_and_is_read_once() -> None:
    raw = (PROBE / "PostToolUse-Read-read-pdf-1.json").read_bytes()
    body = json.loads(raw)["tool_response"]["file"]["base64"]
    seen: list = []

    outcome = process(
        TOOL_EVENTS["PostToolUse"], raw, lambda *_: ENFORCE, _counting(seen)
    )

    assert outcome.output == b""
    # The document is not given a key's context: one pass over it, no more.
    assert [text for text in seen if body in text] == [body]


def _pdf_read(text: str) -> dict:
    return {"type": "pdf", "file": {"filePath": "/w/doc.pdf", "base64": _pdf(text)}}


def test_a_pdf_holding_a_key_is_withheld_saying_what_it_holds() -> None:
    body = _pdf_read(f"aws_access_key_id = {AWS_KEY}")

    outcome = _process("PostToolUse", "Read", body)

    document = json.loads(outcome.output)
    replaced = document["hookSpecificOutput"]["updatedToolOutput"]
    assert replaced["type"] == "text"
    assert replaced["file"]["content"] == WITHHELD
    assert document["systemMessage"] == (
        "shim: withheld this Read result; it holds SECRET (1), which a "
        "placeholder cannot replace without breaking it."
    )
    assert "could not be inspected" not in outcome.output.decode()
    assert body["file"]["base64"] not in outcome.output.decode()
    assert "<SECRET_1>" not in outcome.output.decode()
    assert (outcome.record.action, outcome.record.note) == ("deny", "")
    assert outcome.record.entities == (("SECRET", 1),)


def test_a_pdf_holding_a_key_is_only_reported_under_observe() -> None:
    outcome = _process(
        "PostToolUse", "Read", _pdf_read(f"aws_access_key_id = {AWS_KEY}"), OBSERVE
    )

    assert outcome.output == b""
    assert outcome.record.action == "allow"
    assert outcome.record.entities == (("SECRET", 1),)


def test_a_pdf_without_a_secret_is_left_as_it_is() -> None:
    outcome = _process("PostToolUse", "Read", _pdf_read("Quarterly report"))

    assert outcome.output == b""


def test_colliding_keys_in_a_result_are_withheld_saying_what_they_hold() -> None:
    body = {"tags": {"4111 1111 1111 1111": "vip", "4111111111111111": "key"}}

    outcome = _process("PostToolUse", "mcp__crm__tags", body)

    document = json.loads(outcome.output)
    assert document["hookSpecificOutput"]["updatedToolOutput"] == [
        {"type": "text", "text": WITHHELD}
    ]
    assert "CREDIT_CARD (2)" in document["systemMessage"]
    assert "could not be inspected" not in outcome.output.decode()
    assert b"4111" not in outcome.output


def test_an_image_the_model_will_see_is_not_read_as_text() -> None:
    png = _png()
    seen: list = []
    results = (
        ("mcp__shot__take", [{"type": "image", "data": png, "mimeType": "image/png"}]),
        ("Read", {"type": "image", "file": {"base64": png, "type": "image/png"}}),
        (
            "Read",
            {
                "type": "parts",
                "file": {"filePath": "/w/doc.pdf", "count": 1},
                "pages": [{"base64": png, "mediaType": "image/png"}],
            },
        ),
    )

    for tool, body in results:
        outcome = _process("PostToolUse", tool, body, check=_counting(seen))
        assert outcome.output == b""
    assert seen and not any(png in text for text in seen)


def test_an_image_sent_out_is_still_read() -> None:
    png = _png()
    seen: list = []

    _process(
        "PreToolUse",
        "mcp__files__upload",
        {"attachment": {"type": "image", "data": png, "mimeType": "image/png"}},
        check=_counting(seen),
    )

    assert png in seen


@pytest.mark.parametrize(
    "body",
    (
        {"attachment": {"type": "image", "data": TEXT, "mimeType": "image/png"}},
        {"attachment": {"type": "image", "data": {"note": TEXT}}},
        {"doc": {"type": "image", "file": {"base64": TEXT}}},
    ),
    ids=("data", "data-object", "file-base64"),
)
def test_text_shaped_as_an_image_is_still_refused_on_its_way_out(body) -> None:
    outcome = _process("PreToolUse", "mcp__files__upload", body)

    output = json.loads(outcome.output)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert outcome.record.entities == TEXT_COUNTS


def test_text_shaped_as_an_image_is_never_rewritten_into_one() -> None:
    body = {
        "url": "https://example.com/",
        "prompt": "summarise",
        "extra": {"type": "image", "data": TEXT},
    }

    outcome = _process("PreToolUse", "WebFetch", body)

    output = json.loads(outcome.output)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "updatedInput" not in output
    assert outcome.record.entities == TEXT_COUNTS


@pytest.mark.parametrize(
    ("tool", "body", "withheld"),
    (
        (
            "mcp__x__shot",
            [{"type": "image", "data": TEXT, "mimeType": "image/png"}],
            [{"type": "text", "text": WITHHELD}],
        ),
        (
            "mcp__db__query",
            {"structuredContent": {"row": {"type": "image", "data": TEXT}}},
            [{"type": "text", "text": WITHHELD}],
        ),
        (
            "Read",
            {
                "type": "image",
                "file": {"base64": base64.b64encode(TEXT.encode()).decode()},
            },
            None,
        ),
    ),
    ids=("mcp-block", "structured-content", "read-image"),
)
def test_text_shaped_as_an_image_in_a_result_is_withheld(tool, body, withheld) -> None:
    outcome = _process("PostToolUse", tool, body)

    document = json.loads(outcome.output)
    replaced = document["hookSpecificOutput"]["updatedToolOutput"]
    if withheld is None:
        assert replaced["file"]["content"] == WITHHELD
    else:
        assert replaced == withheld
    assert "which a placeholder cannot replace" in document["systemMessage"]
    assert JANE not in outcome.output.decode()
    assert outcome.record.entities[-1] == ("SECRET", 1)


def test_text_in_a_list_under_an_image_block_is_masked_as_text() -> None:
    outcome = _process(
        "PostToolUse", "mcp__x__shot", [{"type": "image", "data": [JANE]}]
    )

    output = json.loads(outcome.output)["hookSpecificOutput"]
    assert output["updatedToolOutput"] == [{"type": "image", "data": ["<EMAIL_1>"]}]


def test_vscode_reports_text_shaped_as_an_image() -> None:
    body = {"attachment": {"type": "image", "data": TEXT, "mimeType": "image/png"}}

    outcome = _process("PreToolUse", "mcp_files_upload", body, events=VSCODE_EVENTS)

    assert outcome.output != b""
    assert outcome.record.entities == TEXT_COUNTS


def test_text_beside_an_image_is_still_read() -> None:
    png = _png()
    result, _findings, _changed = mask(
        [
            {"type": "image", "data": png, "mimeType": "image/png"},
            {"type": "text", "text": f"sent by {JANE}"},
        ],
        evaluate,
    )

    assert result[1] == {"type": "text", "text": "sent by <EMAIL_1>"}
    assert result[0]["data"] == png


# A real header, then 64K of base64 that a header-only check never reaches.
TAIL = _png() + "A" * 70_000


def test_a_large_image_the_model_will_see_is_not_read_as_text() -> None:
    seen: list = []

    outcome = _process(
        "PostToolUse",
        "mcp__shot__take",
        [{"type": "image", "data": TAIL, "mimeType": "image/png"}],
        check=_counting(seen),
    )

    assert outcome.output == b""
    assert not any(TAIL in text for text in seen)


@pytest.mark.parametrize(
    ("tool", "body"),
    (
        ("mcp__x__shot", [{"type": "image", "data": f"{TAIL} note: {TEXT}"}]),
        (
            "mcp__x__q",
            [
                {"type": "text", "text": "rows"},
                {"type": "image", "data": f"{TAIL} note: {TEXT}"},
            ],
        ),
        ("mcp__db__q", {"rows": [{"type": "image", "data": f"{TAIL} note: {TEXT}"}]}),
        ("Read", {"type": "image", "file": {"base64": f"{TAIL} note: {TEXT}"}}),
    ),
    ids=("mcp-block", "beside-text", "nested", "read-image"),
)
def test_text_after_an_image_header_is_withheld(tool, body) -> None:
    outcome = _process("PostToolUse", tool, body)

    document = json.loads(outcome.output)
    assert "which a placeholder cannot replace" in document["systemMessage"]
    assert JANE not in outcome.output.decode()
    assert outcome.record.entities == TEXT_COUNTS
