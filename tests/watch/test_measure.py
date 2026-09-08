from __future__ import annotations

import json
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
        for counts in memo._counts.values()
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

    filler = "the quick brown fox jumps over the lazy dog. " * 200
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
