from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shim_cli.watch import measure

MESSAGE_START = (
    "event: message_start\n"
    'data: {"type":"message_start","message":{"model":"claude-sonnet-5",'
    '"usage":{"input_tokens":2,"cache_creation_input_tokens":18093,'
    '"cache_read_input_tokens":91562,"output_tokens":5}}}\n\n'
)
MESSAGE_DELTA = (
    "event: message_delta\n"
    'data: {"type":"message_delta","usage":{"output_tokens":214}}\n\n'
)

AT_FILE_BLOCK = (
    "<system-reminder>\n"
    'Called the Read tool with the following input: {"file_path":"/work/notes.txt"}\n'
    "Result of calling the Read tool:\n"
    "1\tALPHA\n2\tBETA\n"
    "</system-reminder>"
)


def _request(**changes) -> dict:
    document = {
        "model": "claude-sonnet-5",
        "system": [{"type": "text", "text": "You are helpful."}],
        "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": "Explain merge sort."}],
        "max_tokens": 4096,
    }
    document.update(changes)
    return document


def test_usage_is_read_from_a_stream_arriving_in_pieces() -> None:
    reader = measure.UsageReader()
    whole = MESSAGE_START + MESSAGE_DELTA
    for index in range(0, len(whole), 7):
        reader.feed(whole[index : index + 7])

    assert reader.usage.input_tokens == 2
    assert reader.usage.cache_creation_input_tokens == 18093
    assert reader.usage.cache_read_input_tokens == 91562
    assert reader.usage.output_tokens == 214


def test_the_total_input_includes_what_the_cache_served() -> None:
    reader = measure.UsageReader()
    reader.feed(MESSAGE_START)

    assert reader.usage.total_input == 2 + 18093 + 91562


def test_the_final_output_count_replaces_the_opening_one() -> None:
    reader = measure.UsageReader()
    reader.feed(MESSAGE_START + MESSAGE_DELTA)

    assert reader.usage.output_tokens == 214


def test_a_stream_that_never_completes_an_event_stays_bounded() -> None:
    reader = measure.UsageReader()
    for _ in range(40):
        reader.feed("x" * 50_000)

    assert len(reader._pending) <= reader.MAX_PENDING


@pytest.mark.parametrize(
    "text",
    ("", "event: ping\n\n", "data: not json\n\n", "data: []\n\n", "data: 7\n\n"),
)
def test_noise_in_the_stream_yields_no_usage(text: str) -> None:
    reader = measure.UsageReader()
    reader.feed(text)

    assert reader.usage == measure.Usage()


def test_sections_are_measured_and_the_rest_is_summed() -> None:
    found = measure.sections(_request())

    assert set(found) == {"tools", "system", "messages", "other"}
    assert all(size > 0 for size in found.values())


def test_attribution_sums_to_the_provider_s_exact_total() -> None:
    by_bytes = {"tools": 139_648, "system": 28_823, "messages": 25_552, "other": 213}

    shares = measure.attribute(by_bytes, 109_657)

    assert sum(shares.values()) == 109_657
    assert shares["tools"] > shares["system"] > shares["messages"]


@pytest.mark.parametrize("total", (1, 7, 999, 109_657))
def test_attribution_never_loses_or_invents_a_token(total: int) -> None:
    by_bytes = {"tools": 3, "system": 3, "messages": 3, "other": 1}

    assert sum(measure.attribute(by_bytes, total).values()) == total


def test_attribution_declines_rather_than_dividing_by_zero() -> None:
    assert measure.attribute({}, 100) == {}
    assert measure.attribute({"tools": 0}, 100) == {}
    assert measure.attribute({"tools": 10}, 0) == {}


def test_an_at_referenced_file_is_counted() -> None:
    document = _request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Summarise @notes.txt"},
                    {"type": "text", "text": AT_FILE_BLOCK},
                ],
            }
        ]
    )

    found = measure.at_files(document)

    assert found.count == 1
    assert found.bytes == len(AT_FILE_BLOCK.encode())


def test_two_at_files_in_one_message_are_both_counted() -> None:
    document = _request(
        messages=[{"role": "user", "content": AT_FILE_BLOCK + "\n\n" + AT_FILE_BLOCK}]
    )

    assert measure.at_files(document).count == 2


def test_an_ordinary_system_reminder_is_not_an_at_file() -> None:
    document = _request(
        messages=[
            {"role": "user", "content": "<system-reminder>Be brief.</system-reminder>"}
        ]
    )

    assert measure.at_files(document).count == 0


def test_an_unterminated_reminder_does_not_hang_or_count() -> None:
    document = _request(
        messages=[{"role": "user", "content": "<system-reminder>" + "x" * 5_000}]
    )

    assert measure.at_files(document).count == 0


def test_an_exchange_keeps_counts_and_sizes_but_no_traffic() -> None:
    secret = "AKIAIOSFODNN7EXAMPLE"
    body = json.dumps(
        _request(messages=[{"role": "user", "content": f"deploy with {secret}"}])
    ).encode()

    from shim_cli.guard import evaluate

    exchange = measure.inspect_request(body, evaluate)

    assert exchange.entities.get("SECRET") == 1
    blob = json.dumps(
        {
            "sections": exchange.sections,
            "entities": exchange.entities,
            "model": exchange.model,
            "path": exchange.path,
            "request_bytes": exchange.request_bytes,
        }
    )
    assert secret not in blob
    assert "merge sort" not in blob


@pytest.mark.parametrize(
    "model", ("claude\nforged-report", "x" * (measure.MAX_MODEL_CHARS + 1))
)
def test_untrusted_model_labels_are_normalized_at_ingress(model: str) -> None:
    exchange = measure.inspect_request(json.dumps(_request(model=model)).encode())

    assert exchange.model == measure.UNKNOWN_MODEL


def test_a_body_past_the_bound_is_counted_but_not_broken_down() -> None:
    body = b"x" * (measure.MAX_BODY_BYTES + 1)

    exchange = measure.inspect_request(body)

    assert exchange.measured is False
    assert exchange.request_bytes == len(body)
    assert exchange.sections == {}


@pytest.mark.parametrize("body", (b"", b"not json", b"\xff\xfe", b"[1,2,3]"))
def test_a_body_that_is_not_a_request_measures_to_nothing(body: bytes) -> None:
    exchange = measure.inspect_request(body)

    assert exchange.sections == {}
    assert exchange.entities == {}


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_usage_delimiter_split_at_every_boundary(newline):
    text = 'data: {"usage":\ndata: {"input_tokens":0,"output_tokens":0}}\n\n'.replace(
        "\n", newline
    )
    for split in range(len(text) + 1):
        reader = measure.UsageReader()
        reader.feed(text[:split])
        reader.feed(text[split:])
        reader.finish()
        assert reader.status == "known"
        assert reader.usage == measure.Usage()


def test_json_usage_and_unavailable_are_distinct():
    reader = measure.UsageReader("application/json; charset=utf-8")
    reader.feed('{"usage":{"input_tokens":0,"output_tokens":0}}')
    reader.finish()
    assert reader.status == "known"
    missing = measure.UsageReader("application/json")
    missing.feed('{"no_usage":true}')
    missing.finish()
    assert missing.status == "unavailable"


def test_nested_json_usage_does_not_escape_measurement():
    reader = measure.UsageReader("application/json")
    reader.feed("[" * 2000 + "0" + "]" * 2000)
    reader.finish()
    assert reader.status == "unavailable"


@pytest.mark.parametrize(
    "document", ['{"usage":{}}', '{"usage":{"input_tokens":true}}', '{"usage":']
)
def test_invalid_or_missing_usage_is_unavailable(document):
    reader = measure.UsageReader("Application/JSON")
    reader.feed(document)
    reader.finish()
    assert reader.status == "unavailable"


IBAN = "TR330006100519786457841326"


class _Recorder:
    """Stands in for the detector so the tests can count how often it ran."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str):
        self.calls.append(text)
        return SimpleNamespace(counts=())


def _placed() -> dict:
    """One synthetic IBAN in each place the wire can carry text the model reads."""
    return _request(
        system=[
            {"type": "text", "text": f"policy {IBAN}"},
            {"type": "text", "text": f"memory {IBAN}"},
        ],
        tools=[
            {
                "name": "Read",
                "description": f"reads a file, for example {IBAN}",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": f"path {IBAN}"}
                    },
                },
            }
        ],
        messages=[
            {"role": "user", "content": f"plain {IBAN}"},
            {"role": "user", "content": [{"type": "text", "text": f"part {IBAN}"}]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [{"type": "text", "text": f"nested {IBAN}"}],
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_2",
                        "content": f"string {IBAN}",
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_3",
                        "name": "Bash",
                        "input": {"command": f"echo {IBAN}"},
                    }
                ],
            },
        ],
    )


def _ibans(exchange) -> dict:
    return {
        name: counts["IBAN"]
        for name, counts in exchange.entities_by_section.items()
        if "IBAN" in counts
    }


def test_every_text_field_the_model_reads_is_scanned() -> None:
    from shim_cli.guard import evaluate

    exchange = measure.inspect_request(json.dumps(_placed()).encode(), evaluate)

    assert _ibans(exchange) == {"messages": 5, "system": 2, "tools": 2}
    assert exchange.entities["IBAN"] == 9
    assert exchange.measured


def test_a_system_prompt_sent_as_a_string_is_scanned() -> None:
    from shim_cli.guard import evaluate

    body = json.dumps(_request(system=f"policy {IBAN}")).encode()

    assert _ibans(measure.inspect_request(body, evaluate)) == {"system": 1}


def test_base64_payloads_and_thinking_signatures_are_left_alone() -> None:
    from shim_cli.guard import evaluate

    document = _request(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": IBAN,
                        },
                    },
                    {"type": "thinking", "thinking": "nothing", "signature": IBAN},
                ],
            }
        ]
    )

    exchange = measure.inspect_request(json.dumps(document).encode(), evaluate)

    assert "IBAN" not in exchange.entities
    assert exchange.measured


def test_a_data_field_outside_a_base64_block_is_still_scanned() -> None:
    from shim_cli.guard import evaluate

    document = _request(
        messages=[{"role": "user", "content": [{"type": "text", "data": IBAN}]}]
    )

    exchange = measure.inspect_request(json.dumps(document).encode(), evaluate)

    assert _ibans(exchange) == {"messages": 1}


def test_an_unchanged_tools_section_is_scanned_once_per_session() -> None:
    memo = measure.SectionMemo()
    record = _Recorder()
    first = {"name": "Read", "description": "first tool set"}
    second = {"name": "Write", "description": "second tool set"}

    for tool, prompt in ((first, "one"), (first, "two"), (second, "three")):
        body = json.dumps(
            _request(tools=[tool], messages=[{"role": "user", "content": prompt}])
        ).encode()
        measure.inspect_request(body, record, memo)

    assert record.calls.count("first tool set") == 1
    assert record.calls.count("second tool set") == 1
    assert [call for call in record.calls if call in ("one", "two", "three")] == [
        "one",
        "two",
        "three",
    ]


def test_without_a_memo_every_request_is_scanned_in_full() -> None:
    record = _Recorder()
    body = json.dumps(
        _request(tools=[{"name": "Read", "description": "same"}])
    ).encode()

    measure.inspect_request(body, record)
    measure.inspect_request(body, record)

    assert record.calls.count("same") == 2


def test_the_memo_is_bounded_and_keeps_counts_rather_than_text() -> None:
    from shim_cli.guard import evaluate

    memo = measure.SectionMemo()
    for index in range(measure.MEMO_LIMIT + 10):
        body = json.dumps(
            _request(tools=[{"name": f"tool-{index}", "description": f"see {IBAN}"}])
        ).encode()
        measure.inspect_request(body, evaluate, memo)

    assert len(memo._counts) <= measure.MEMO_LIMIT
    stored = json.dumps(memo._counts)
    assert IBAN not in stored
    assert "tool-0" not in stored
    assert all(
        isinstance(entity, str) and isinstance(count, int)
        for counts, _named in memo._counts.values()
        for entity, count in counts.items()
    )


def test_a_request_past_the_leaf_limit_is_reported_as_unmeasured(monkeypatch) -> None:
    from shim_cli.guard import evaluate

    monkeypatch.setattr(measure, "MAX_SCAN_LEAVES", 8)
    document = _request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"note {index} {IBAN}"}
                    for index in range(20)
                ],
            }
        ]
    )

    exchange = measure.inspect_request(json.dumps(document).encode(), evaluate)

    assert exchange.measured is False
    assert exchange.entities == {}
    assert exchange.entities_by_section == {}


def test_a_session_sized_request_stays_within_the_proxy_budget() -> None:
    """A real one-turn request measured 169,134 characters over 921 leaves.

    That is 85% of the hook's character budget before the conversation starts,
    so the proxy carries its own, bounded by the body cap it already enforces.
    """
    from shim_cli.events.payload import MAX_TEXT_CHARACTERS, walk

    # Big enough to exceed the hook's character budget, which R3 raised to
    # the hook's own input bound; the proxy's is larger still.
    filler = "the quick brown fox jumps over the lazy dog. " * 600
    document = _request(
        tools=[
            {"name": f"tool-{index}", "description": filler, "input_schema": {}}
            for index in range(20)
        ],
        messages=[
            {"role": "user", "content": f"turn {index} {filler}"} for index in range(20)
        ],
    )
    reachable = walk(document, max_leaves=10**6, max_characters=10**9)
    assert reachable.characters > MAX_TEXT_CHARACTERS

    exchange = measure.inspect_request(json.dumps(document).encode(), _Recorder())

    assert exchange.measured is True


def test_a_tool_schema_deeper_than_the_hook_allows_is_still_measured() -> None:
    """One measured request nested 35 deep: an MCP tool's recursive filter schema."""
    from shim_cli.events.payload import MAX_DEPTH
    from shim_cli.guard import evaluate

    schema: dict = {"description": f"see {IBAN}"}
    for _ in range(MAX_DEPTH):
        schema = {"properties": {"value": schema}}
    body = json.dumps(
        _request(tools=[{"name": "Deep", "input_schema": schema}])
    ).encode()

    exchange = measure.inspect_request(body, evaluate)

    assert exchange.measured is True
    assert _ibans(exchange) == {"tools": 1}


@pytest.mark.parametrize(
    ("document", "expected"),
    (
        (
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
            "max_tokens",
        ),
        ({"stop_reason": "end_turn"}, "end_turn"),
        ({"message": {"stop_reason": "tool_use"}}, "tool_use"),
        (
            {
                "type": "response.completed",
                "response": {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
            },
            "max_output_tokens",
        ),
        (
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
            },
            "content_filter",
        ),
        ({"response": {"status": "completed"}}, ""),
        ({"choices": [{"finish_reason": "length"}]}, "length"),
        ({"choices": [{"finish_reason": "stop"}]}, "stop"),
        ({"choices": []}, ""),
        ({"choices": "not a list"}, ""),
        ({}, ""),
        ([], ""),
        ({"stop_reason": 7}, ""),
        ({"stop_reason": ""}, ""),
        ({"stop_reason": "x" * 41}, measure.UNKNOWN_REASON),
        ({"stop_reason": "max\ntokens"}, measure.UNKNOWN_REASON),
    ),
)
def test_the_provider_s_stop_reason_is_read_in_every_shape(document, expected) -> None:
    assert measure.stop_reason_from(document) == expected


def test_usage_and_a_stop_reason_in_one_event_are_both_kept() -> None:
    reader = measure.UsageReader()

    reader.feed(
        "event: message_delta\n"
        'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},'
        '"usage":{"output_tokens":214}}\n\n'
    )

    assert reader.stop_reason == "max_tokens"
    assert reader.usage.output_tokens == 214


def test_the_first_stop_reason_on_the_wire_wins() -> None:
    reader = measure.UsageReader()

    reader.feed('data: {"delta":{"stop_reason":"max_tokens"}}\n\n')
    reader.feed('data: {"delta":{"stop_reason":"end_turn"}}\n\n')

    assert reader.stop_reason == "max_tokens"


def test_a_stream_that_never_states_a_reason_leaves_it_empty() -> None:
    reader = measure.UsageReader()

    reader.feed(MESSAGE_START + MESSAGE_DELTA)

    assert reader.stop_reason == ""
    assert measure.Exchange().stop_reason == ""


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "watch"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fed(name: str, content_type: str = "text/event-stream") -> measure.UsageReader:
    reader = measure.UsageReader(content_type)
    reader.feed(_read(name))
    reader.finish()
    return reader


def test_a_value_split_across_two_deltas_is_rejoined_before_it_is_scanned() -> None:
    from shim_cli.guard import evaluate

    reader = _fed("split-iban.sse")

    assert reader.response_texts() == [
        ("text", "the account is TR330006100519786457841326 as recorded")
    ]
    assert measure.scan_response(reader, evaluate)[0] == {"text": {"IBAN": 1}}
    assert reader.response_status == "known"


def test_thinking_is_counted_apart_from_the_answer() -> None:
    from shim_cli.guard import evaluate

    reader = _fed("thinking.sse")

    assert measure.scan_response(reader, evaluate)[0] == {"thinking": {"IBAN": 1}}
    assert dict(reader.response_texts())["text"] == "I removed the duplicate."


def test_a_tool_call_the_model_wrote_is_not_counted_here() -> None:
    from shim_cli.guard import evaluate

    reader = _fed("tool-input.sse")

    assert reader.response_texts() == []
    assert measure.scan_response(reader, evaluate)[0] == {}
    assert reader.stop_reason == "tool_use"


def test_an_address_the_model_wrote_is_counted_as_model_text() -> None:
    from shim_cli.guard import evaluate

    reader = _fed("email-in-text.sse")

    assert measure.scan_response(reader, evaluate)[0] == {"text": {"EMAIL": 1}}


def test_a_plain_json_response_yields_its_blocks_directly() -> None:
    from shim_cli.guard import evaluate

    reader = _fed("body.json", "application/json")

    assert measure.scan_response(reader, evaluate)[0] == {
        "thinking": {"IBAN": 1},
        "text": {"EMAIL": 1},
    }
    assert reader.response_status == "known"


def test_a_response_past_the_cap_is_partial_and_stays_bounded() -> None:
    reader = measure.UsageReader()
    delta = (
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"%s"}}\n\n'
    )
    for _ in range(12):
        reader.feed(delta % ("x" * 100_000))
    reader.finish()

    kept = sum(len(text) for _kind, text in reader.response_texts())
    assert kept == measure.MAX_RESPONSE_CHARACTERS
    assert reader.response_status == "partial"


def test_a_body_that_cannot_be_framed_reports_no_response_scan() -> None:
    reader = measure.UsageReader("text/html")
    reader.feed("<html>TR330006100519786457841326</html>")
    reader.finish()

    assert reader.response_texts() == []
    assert reader.response_status == "unavailable"


def test_an_event_that_will_not_parse_leaves_the_scan_partial() -> None:
    reader = measure.UsageReader()
    reader.feed(_read("email-in-text.sse"))
    reader.feed("data: {not json\n\n")
    reader.finish()

    assert reader.response_status == "partial"


def test_the_accumulated_response_is_dropped_on_request() -> None:
    reader = _fed("split-iban.sse")

    reader.forget()

    assert reader.response_texts() == []


def test_one_oversized_leaf_no_longer_voids_the_whole_request() -> None:
    """`evaluate` refused a leaf past its single-pass limit, the exception left
    `_tally`, and the request came back `measured=False` — a session with one
    large tool result measured as nothing at all."""
    import json as _json

    from shim_cli.guard import evaluate as _evaluate
    from shim_cli.watch.measure import inspect_request

    big = "".join(f"row {i} user{i}@example.com\n" for i in range(9_000))
    assert len(big) > 250_000
    body = _json.dumps(
        {
            "model": "claude-opus-5",
            "messages": [
                {"role": "user", "content": [{"type": "tool_result", "content": big}]}
            ],
        }
    ).encode()

    exchange = inspect_request(body, _evaluate)

    assert exchange.measured is True
    assert exchange.entities_by_section["messages"]["EMAIL"] == 9_000
