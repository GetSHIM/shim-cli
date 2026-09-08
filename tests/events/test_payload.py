from __future__ import annotations

import json

import pytest

from shim_cli.events import payload
from shim_cli.events.payload import PayloadTooLarge, mask, replace, walk
from shim_cli.guard import evaluate

NESTED = {
    "tool_response": [
        {"type": "text", "text": "Contact alice@example.com"},
        {"type": "text", "text": "nothing here"},
    ],
    "meta": {"count": 2, "ok": True, "ratio": 0.5, "empty": None, "blank": ""},
    "tags": ["a", ""],
}


def test_walk_finds_string_leaves_in_document_order() -> None:
    found = walk(NESTED)

    assert [path for path, _ in found.leaves] == [
        ("tool_response", 0, "type"),
        ("tool_response", 0, "text"),
        ("tool_response", 1, "type"),
        ("tool_response", 1, "text"),
        ("tags", 0),
    ]
    assert found.characters == sum(len(text) for _, text in found.leaves)


def test_walk_skips_empty_strings_and_non_string_scalars() -> None:
    found = walk({"a": "", "b": None, "c": 1, "d": True, "e": 2.5})

    assert found.leaves == []


def test_replace_rebuilds_the_same_shape_without_changing_types() -> None:
    result = replace(NESTED, {("tool_response", 0, "text"): "Contact <EMAIL_1>"})

    assert result["tool_response"][0]["text"] == "Contact <EMAIL_1>"
    assert result["tool_response"][1] == NESTED["tool_response"][1]
    assert result["meta"] == NESTED["meta"]
    assert type(result["meta"]["count"]) is int
    assert result["meta"]["ok"] is True
    assert result["meta"]["empty"] is None
    assert isinstance(result["tool_response"], list)
    assert NESTED["tool_response"][0]["text"] == "Contact alice@example.com"


def test_replace_refuses_to_change_a_leaf_to_another_type() -> None:
    with pytest.raises(TypeError, match="only be replaced by a string"):
        replace(NESTED, {("tool_response", 0, "text"): {"masked": True}})


def test_mask_rewrites_only_the_leaves_that_carry_findings() -> None:
    result, findings, changed = mask(NESTED, evaluate)

    assert changed is True
    assert [path for path, _ in findings] == [("tool_response", 0, "text")]
    assert result["tool_response"][0]["text"] == "Contact <EMAIL_1>"
    assert json.loads(json.dumps(result)) == result


def test_mask_returns_the_original_object_when_nothing_is_found() -> None:
    clean = {"a": {"b": "nothing sensitive"}}
    result, findings, changed = mask(clean, evaluate)

    assert result is clean
    assert (findings, changed) == ([], False)


def test_ordinals_restart_for_each_leaf() -> None:
    document = {
        "first": "Contact alice@example.com",
        "second": "Contact bob@example.org",
    }
    result, _findings, _changed = mask(document, evaluate)

    assert result == {"first": "Contact <EMAIL_1>", "second": "Contact <EMAIL_1>"}


def test_deep_nesting_is_refused_rather_than_partially_scanned() -> None:
    document: object = "Contact alice@example.com"
    for _ in range(payload.MAX_DEPTH + 2):
        document = {"next": document}

    with pytest.raises(PayloadTooLarge, match="nested more deeply"):
        walk(document)


def test_a_caller_off_the_hook_path_may_raise_the_limits() -> None:
    document: object = "Contact alice@example.com"
    for _ in range(payload.MAX_DEPTH + 2):
        document = {"next": document}

    found = walk(document, max_depth=payload.MAX_DEPTH + 4)

    assert len(found.leaves) == 1


def test_oversized_text_is_refused_rather_than_partially_scanned() -> None:
    document = {"chunk": "x" * (payload.MAX_TEXT_CHARACTERS + 1)}

    with pytest.raises(PayloadTooLarge, match="exceeds the safe analysis limit"):
        walk(document)


def test_too_many_leaves_are_refused() -> None:
    document = {str(index): "value" for index in range(payload.MAX_LEAVES + 2)}

    with pytest.raises(PayloadTooLarge, match="too many text fields"):
        walk(document)


def test_a_real_read_result_is_masked_in_place() -> None:
    document = {
        "type": "text",
        "file": {
            "filePath": "/probe/workspace/dotenv-sample.txt",
            "content": "DATABASE_URL=postgresql://alice:pw@db.example.com/app\n",
            "numLines": 1,
            "totalLines": 1,
            "truncatedByTokenCap": False,
        },
    }
    result, findings, changed = mask(document, evaluate)

    assert changed is True
    assert result["file"]["content"] == "DATABASE_URL=<DB_URI_1>\n"
    assert result["file"]["numLines"] == 1
    assert result["file"]["truncatedByTokenCap"] is False
    assert result["type"] == "text"
    assert len(findings) == 1


def test_partial_inspection_stops_at_deadline_and_keeps_completed_redactions():
    seen = []

    def deadline(text):
        seen.append(text)
        if text == "timeout":
            raise ValueError("analysis limit") from TimeoutError("hook deadline")
        return evaluate(text)

    result = payload.inspect(["alice@example.com", "timeout", "never scan"], deadline)
    assert seen == ["alice@example.com", "timeout"]
    assert result.value == ["<EMAIL_1>", "timeout", "never scan"]
    assert result.status == "partial"
    assert result.skipped == 2
    assert result.reasons == ("deadline",)
