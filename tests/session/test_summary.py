from __future__ import annotations

import json

import pytest

from shim_cli.session import summary


def _record(**changes: object) -> dict:
    record = {
        "client": "claude",
        "event": "PostToolUse",
        "tool_name": "Read",
        "target": "/work/service/.env",
        "action": "mask",
        "entities": {"SECRET": 1},
        "latency_ms": 6,
    }
    record.update(changes)
    return record


def test_a_session_where_nothing_happened_says_nothing() -> None:
    assert summary.render([]) == ""
    assert summary.render([_record(action="allow", entities={})]) == ""


def test_the_summary_groups_by_action_then_entity() -> None:
    text = summary.render(
        [
            _record(entities={"SECRET": 2}),
            _record(tool_name="Bash", target="", entities={"SECRET": 1}),
            _record(target="/work/docker-compose.yml", entities={"DB_URI": 2}),
            _record(
                event="UserPromptSubmit",
                tool_name="",
                target="",
                action="report",
                entities={"EMAIL": 1},
                latency_ms=14,
            ),
            _record(tool_name="Bash", target="", action="deny", entities={"SECRET": 1}),
            _record(action="allow", entities={}, latency_ms=4),
        ]
    )

    assert text.splitlines() == [
        "shim — this session",
        "  masked    3 SECRET  (Bash, Read .env)",
        "            2 DB_URI  (Read docker-compose.yml)",
        "  blocked   1 SECRET  (Bash)",
        "  warned    1 EMAIL  (your prompt)",
        "  overhead  6 ms median, 14 ms p95",
    ]


def test_only_the_file_name_is_shown_not_the_path() -> None:
    text = summary.render([_record(target="/home/someone/private/project/.env")])

    assert "(Read .env)" in text
    assert "/home/someone" not in text


def test_a_url_target_is_kept_whole() -> None:
    text = summary.render(
        [_record(tool_name="WebFetch", target="https://example.com/a/b")]
    )

    assert "(WebFetch https://example.com/a/b)" in text


def test_many_places_are_counted_rather_than_listed() -> None:
    records = [
        _record(target=f"/work/file{index}.env", entities={"SECRET": 1})
        for index in range(6)
    ]

    text = summary.render(records)

    assert "+3 more" in text
    assert text.count("Read file") == summary.MAX_SOURCES


def test_a_capped_session_says_the_count_is_short() -> None:
    text = summary.render([_record()], capped=True)

    assert "size cap" in text


@pytest.mark.parametrize("field", ["entities", "latency_ms", "action"])
def test_a_record_missing_a_field_does_not_break_the_summary(field: str) -> None:
    record = _record()
    del record[field]

    summary.render([record, _record()])


def test_json_carries_the_same_facts_as_the_text() -> None:
    records = [
        _record(entities={"SECRET": 2}),
        _record(action="report", entities={"EMAIL": 1}, latency_ms=14),
        _record(action="allow", entities={}),
    ]

    document = summary.as_json(records)

    assert document["events"] == 3
    assert document["acted"] == 2
    assert document["actions"]["mask"]["entities"] == {"SECRET": 2}
    assert document["actions"]["report"]["entities"] == {"EMAIL": 1}
    assert document["overhead_ms"] == {"median": 6, "p95": 14}
    assert document["capped"] is False


def test_even_latency_samples_use_the_actual_median() -> None:
    document = summary.as_json([_record(latency_ms=2), _record(latency_ms=8)])

    assert document["overhead_ms"] == {"median": 5, "p95": 8}


def test_malformed_latency_samples_are_ignored() -> None:
    malformed = (float("nan"), float("inf"), -1, True, 10**1000)
    records = [_record(latency_ms=value) for value in malformed]

    assert summary.as_json([*records, _record()])["overhead_ms"] == {
        "median": 6,
        "p95": 6,
    }


def test_a_record_with_unreadable_counts_produces_no_heading() -> None:
    assert summary.render([_record(entities=["SECRET"])]) == ""
    assert summary.render([_record(entities=None)]) == ""
    assert summary.render([_record(entities={})]) == ""


def test_one_unreadable_record_does_not_hide_a_readable_one() -> None:
    text = summary.render(
        [_record(entities=["SECRET"]), _record(entities={"EMAIL": 1})]
    )

    assert "1 EMAIL" in text
    assert text.startswith("shim — this session")


def test_malformed_source_fields_do_not_hide_or_leak_readable_records() -> None:
    records = [
        _record(
            event=["LEAK"],
            tool_name=["LEAK"],
            target={"LEAK": True},
        ),
        _record(entities={"EMAIL": 1}),
    ]

    text = summary.render(records)
    document = summary.as_json(records)

    assert "1 EMAIL" in text
    assert document["actions"]["mask"]["sources"] == ["Read .env"]
    assert "LEAK" not in text + json.dumps(document)


def _flagged(**changes: object) -> dict:
    defaults: dict = {
        "action": "allow",
        "entities": {},
        "markers": ["INSTRUCTION_OVERRIDE"],
        "target": "/work/vendor/README.md",
    }
    defaults.update(changes)
    return _record(**defaults)


def test_a_marker_names_the_file_it_came_from() -> None:
    text = summary.render([_flagged()])

    assert "flagged   1 INSTRUCTION_OVERRIDE  (Read README.md)" in text


def test_markers_alone_are_worth_a_summary() -> None:
    assert summary.render([_flagged()]) != ""


def test_the_flagged_label_is_written_once_per_block() -> None:
    text = summary.render(
        [_flagged(markers=["INSTRUCTION_OVERRIDE", "HIDDEN_TEXT", "SECRECY_REQUEST"])]
    )

    assert text.count("flagged") == 1
    assert "INSTRUCTION_OVERRIDE" in text
    assert "HIDDEN_TEXT" in text


def test_one_marker_from_several_files_names_them_all() -> None:
    text = summary.render(
        [
            _flagged(target="/work/a.md"),
            _flagged(target="/work/b.md"),
        ]
    )

    assert "2 INSTRUCTION_OVERRIDE" in text
    assert "Read a.md" in text
    assert "Read b.md" in text


def test_marker_sources_reach_the_json_report() -> None:
    document = summary.as_json([_flagged()])

    assert document["markers"] == {
        "INSTRUCTION_OVERRIDE": {"count": 1, "sources": ["Read README.md"]}
    }


def test_a_marker_never_carries_the_text_that_matched_it() -> None:
    text = summary.render([_flagged(target="/work/notes.md")])
    document = summary.as_json([_flagged(target="/work/notes.md")])

    assert "ignore all previous" not in text.lower()
    assert "ignore" not in json.dumps(document).lower()
