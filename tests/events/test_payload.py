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


def test_walk_finds_keys_text_and_numbers_in_document_order() -> None:
    found = walk(NESTED)

    # A key's leaf is `(key,)` after the path of the object that holds it.
    assert found.leaves == [
        ((("tool_response",),), "tool_response"),
        (("tool_response", 0, ("type",)), "type"),
        (("tool_response", 0, "type"), "text"),
        (("tool_response", 0, ("text",)), "text"),
        (("tool_response", 0, "text"), "Contact alice@example.com"),
        (("tool_response", 1, ("type",)), "type"),
        (("tool_response", 1, "type"), "text"),
        (("tool_response", 1, ("text",)), "text"),
        (("tool_response", 1, "text"), "nothing here"),
        ((("meta",),), "meta"),
        (("meta", ("count",)), "count"),
        (("meta", "count"), "2"),
        (("meta", ("ok",)), "ok"),
        (("meta", ("ratio",)), "ratio"),
        (("meta", "ratio"), "0.5"),
        (("meta", ("empty",)), "empty"),
        (("meta", ("blank",)), "blank"),
        ((("tags",),), "tags"),
        (("tags", 0), "a"),
    ]
    assert found.characters == sum(len(text) for _, text in found.leaves)


def test_walk_skips_empty_text_flags_and_nulls() -> None:
    found = walk({"": "x", "a": "", "b": None, "c": True, "d": False})

    assert found.leaves == [
        (("",), "x"),
        ((("a",),), "a"),
        ((("b",),), "b"),
        ((("c",),), "c"),
        ((("d",),), "d"),
    ]


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


def test_ordinals_run_on_across_the_leaves_of_one_event() -> None:
    # They restarted for each leaf, so two addresses were both <EMAIL_1>.
    document = {
        "first": "Contact alice@example.com",
        "second": "Contact bob@example.org",
    }
    result, _findings, _changed = mask(document, evaluate)

    assert result == {"first": "Contact <EMAIL_1>", "second": "Contact <EMAIL_2>"}


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

    # One leaf for each `next` key, and the text at the bottom.
    assert len(found.leaves) == payload.MAX_DEPTH + 3
    assert found.leaves[-1] == (
        ("next",) * (payload.MAX_DEPTH + 2),
        "Contact alice@example.com",
    )


def test_oversized_text_is_refused_rather_than_partially_scanned() -> None:
    document = {"chunk": "x" * (payload.MAX_TEXT_CHARACTERS + 1)}

    with pytest.raises(PayloadTooLarge, match="exceeds the safe analysis limit"):
        walk(document)


def test_a_caller_that_bounds_leaves_has_too_many_refused() -> None:
    document = {str(index): "value" for index in range(12)}

    with pytest.raises(PayloadTooLarge, match="too many text fields"):
        walk(document, max_leaves=10)


def test_the_hook_walk_has_no_leaf_count_to_skip_past() -> None:
    """Its input is already bounded at 1 MB; a count below that only turned
    the fields after it into fields nobody inspected."""
    document = ["value"] * 20_000

    found = walk(document, partial=True)

    assert len(found.leaves) == 20_000
    assert (found.skipped, found.reasons) == (0, set())


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
    assert result["file"]["content"] == (
        "DATABASE_URL=postgresql://<DB_URI_1>@db.example.com/app\n"
    )
    assert result["file"]["numLines"] == 1
    assert result["file"]["truncatedByTokenCap"] is False
    assert result["type"] == "text"
    assert len(findings) == 1


def test_inspection_stops_at_the_deadline_and_hands_back_nothing_to_emit():
    seen = []

    def deadline(text, **kwargs):
        seen.append(text)
        if text == "timeout":
            raise ValueError("analysis limit") from TimeoutError("hook deadline")
        return evaluate(text, **kwargs)

    result = payload.inspect(["alice@example.com", "timeout", "never scan"], deadline)
    assert seen == ["alice@example.com", "timeout"]
    # "never scan" was never scanned, so no value carrying it may be emitted.
    assert (result.value, result.changed) == (None, False)
    assert [path for path, _ in result.findings] == [(0,)]
    assert result.status == "partial"
    assert result.skipped == 2
    assert result.reasons == ("deadline",)
