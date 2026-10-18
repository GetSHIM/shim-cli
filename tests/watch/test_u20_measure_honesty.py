"""What `shim watch` measures, and what it says when it could not."""

from __future__ import annotations

import json

from shim_cli.guard import evaluate
from shim_cli.watch import measure, report

EMAIL = "jane.doe@example.com"
CARD = "4111111111111111"
PERSON = f"Jane Doe | {EMAIL} | card {CARD}"


def _read_in_part(unanalysable: str) -> str:
    """Past one piece: the email is read, the piece holding `unanalysable` not."""
    return f"{EMAIL}\n{'x' * 100_000}\n{unanalysable}"


def _body(**changes) -> bytes:
    document = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "send the invoice"}],
    }
    document.update(changes)
    return json.dumps(document).encode()


def _tool_call(argument: str) -> list:
    return [
        {"role": "user", "content": "send the invoice"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "send_email",
                    "input": {"to": "billing", argument: PERSON},
                }
            ],
        },
    ]


def test_a_tool_argument_named_signature_is_counted() -> None:
    exchange = measure.inspect_request(
        _body(messages=_tool_call("signature")), evaluate
    )

    assert exchange.entities == {"EMAIL": 1, "CREDIT_CARD": 1}
    assert exchange.measured


def test_only_a_thinking_block_s_signature_is_skipped() -> None:
    thinking = {"type": "thinking", "thinking": "hm", "signature": PERSON}
    elsewhere = {"type": "text", "text": "ok", "signature": PERSON}
    skipped = measure.inspect_request(
        _body(messages=[{"role": "assistant", "content": [thinking]}]), evaluate
    )
    counted = measure.inspect_request(
        _body(messages=[{"role": "assistant", "content": [elsewhere]}]), evaluate
    )

    assert skipped.entities == {}
    assert counted.entities == {"EMAIL": 1, "CREDIT_CARD": 1}


def test_a_leaf_the_detector_refuses_marks_the_request_not_fully_analysed(
    unanalysable: str,
) -> None:
    body = _body(
        messages=[
            {"role": "user", "content": f"{unanalysable} {EMAIL}"},
            {"role": "user", "content": f"card {CARD}"},
        ]
    )

    exchange = measure.inspect_request(body, evaluate)

    assert not exchange.measured
    assert exchange.incomplete_reason == measure.NOT_ANALYSED
    assert exchange.entities == {"CREDIT_CARD": 1}


def test_a_leaf_analysed_in_part_is_not_reported_as_a_whole_count(
    unanalysable: str,
) -> None:
    body = _body(messages=[{"role": "user", "content": _read_in_part(unanalysable)}])

    exchange = measure.inspect_request(body, evaluate)

    assert not exchange.measured
    assert exchange.incomplete_reason == measure.NOT_ANALYSED


def test_a_partial_decision_is_not_a_complete_count_for_a_memoised_section(
    unanalysable: str,
) -> None:
    memo = measure.SectionMemo()
    body = _body(system=_read_in_part(unanalysable))

    first = measure.inspect_request(body, evaluate, memo)
    again = measure.inspect_request(body, evaluate, memo)

    assert not first.measured and not again.measured
    assert again.incomplete_reason == measure.NOT_ANALYSED


def test_the_report_names_the_cause_of_a_partial_analysis(unanalysable: str) -> None:
    session = _session(measure.inspect_request(_body(system=unanalysable), evaluate))

    text = report.render(session, 1)

    assert "inspection incomplete for 1 request(s): 1 held text" in text
    assert "could not be read as JSON" not in text


def test_a_response_the_detector_refuses_is_reported_not_as_clean(
    unanalysable: str,
) -> None:
    reader = measure.UsageReader("text/event-stream")
    reader.feed(
        'data: {"type":"content_block_delta","index":0,"delta":'
        f'{{"type":"text_delta","text":"{unanalysable}"}}}}\n\n'
    )

    counts, _named, partial = measure.scan_response(reader, evaluate)

    assert counts == {}
    assert partial


def test_sections_never_add_up_to_more_than_the_request() -> None:
    tools = [
        {"name": f"tool_{n}", "input_schema": {"type": "object", "properties": {}}}
        for n in range(200)
    ]
    body = json.dumps(
        {"model": "claude-sonnet-5", "tools": tools, "messages": []},
        separators=(",", ":"),
    ).encode()

    exchange = measure.inspect_request(body)

    assert sum(exchange.sections.values()) <= len(body)
    assert exchange.sections["tools"] == len(json.dumps(tools, separators=(",", ":")))


def test_a_delta_of_an_unexpected_shape_is_ignored_not_raised() -> None:
    reader = measure.UsageReader("text/event-stream")
    for event in (
        {"type": "content_block_delta", "index": 0, "delta": {"type": ["x"]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": {"a": 1}}},
        {
            "type": "content_block_delta",
            "index": [0],
            "delta": {"type": "text_delta", "text": "x"},
        },
    ):
        reader.feed(f"data: {json.dumps(event)}\n\n")
    reader.finish()

    assert reader.response_texts() == []


def test_a_json_body_of_an_unexpected_shape_is_ignored_not_raised() -> None:
    reader = measure.UsageReader("application/json")
    reader.feed(json.dumps({"type": "content_block_delta", "delta": {"type": {}}}))

    reader.finish()

    assert reader.response_texts() == []


def _session(exchange):
    from shim_cli.watch import proxy

    exchange.path = "/v1/messages"
    exchange.status = 200
    session = proxy.Session()
    session.record(exchange)
    return session
