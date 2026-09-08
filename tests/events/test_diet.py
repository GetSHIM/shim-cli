from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from shim_cli.events import diet

ROOT = Path(__file__).parents[2]

PRETTY_JSON = json.dumps(
    {
        "items": [
            {"id": index, "name": f"item-{index}", "tags": ["alpha", "beta"]}
            for index in range(8)
        ],
        "total": 8,
    },
    indent=4,
)

SAMPLES = (
    PRETTY_JSON,
    '{\n  "a": [1, 2, 3],\n  "b": {"c": "d"}\n}',
    "line one   \nline two\t\nline three" + " " * 40,
    "plain prose that is not json at all and has no trailing space to remove",
    '{"already":"compact","and":["quite","small"]}',
    "",
    "   ",
    "{",
    '{"unterminated": "string',
    "[1, 2, 3]" + " " * 80,
)


@pytest.mark.parametrize("text", SAMPLES)
def test_every_transform_is_idempotent(text: str) -> None:
    for name in diet.TRANSFORMS:
        once, _ = diet.shrink(text, (name,))
        twice, _ = diet.shrink(once, (name,))
        assert twice == once, name

    once, _ = diet.shrink(text)
    twice, _ = diet.shrink(once)
    assert twice == once


@pytest.mark.parametrize("text", SAMPLES)
def test_every_transform_is_deterministic_within_a_process(text: str) -> None:
    assert diet.shrink(text) == diet.shrink(text)


def test_transforms_are_deterministic_across_processes() -> None:
    source = (
        "import json,sys\n"
        "from shim_cli.events import diet\n"
        "samples = json.loads(sys.stdin.read())\n"
        "sys.stdout.write(json.dumps([diet.shrink(t) for t in samples]))\n"
    )
    runs = []
    for seed in ("0", "1", "12345"):
        result = subprocess.run(
            (sys.executable, "-I", "-B", "-c", source),
            input=json.dumps(list(SAMPLES)).encode(),
            capture_output=True,
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "src"),
                "PYTHONHASHSEED": seed,
            },
            check=False,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr.decode()
        runs.append(result.stdout)

    assert len(set(runs)) == 1


@pytest.mark.parametrize("text", SAMPLES)
def test_a_transform_never_makes_text_longer(text: str) -> None:
    shrunk, _applied = diet.shrink(text)

    assert len(shrunk) <= len(text)


def test_json_compaction_preserves_the_parsed_value() -> None:
    compacted = diet.compact_json(PRETTY_JSON)

    assert len(compacted) < len(PRETTY_JSON)
    assert json.loads(compacted) == json.loads(PRETTY_JSON)


@pytest.mark.parametrize(
    "literal",
    [
        "1.10",
        "1e5",
        "1E+5",
        "12345678901234567890123456789",
        "0.1000000000000000055511151231257827",
        "-0.0",
    ],
)
def test_json_compaction_keeps_number_literals_verbatim(literal: str) -> None:
    text = (
        '{\n  "value": ' + literal + ',\n  "padding": "aaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n}'
    )

    compacted = diet.compact_json(text)

    assert (
        compacted
        == '{"value":' + literal + ',"padding":"aaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
    )


def test_json_compaction_never_touches_string_contents() -> None:
    text = (
        '{\n  "code": "def f():\\n    return 1  # two spaces   ",\n  "x": "  keep  "\n}'
    )

    compacted = diet.compact_json(text)

    assert json.loads(compacted) == json.loads(text)
    assert '"  keep  "' in compacted


def test_json_compaction_preserves_duplicate_keys() -> None:
    text = '{\n  "a": 1,\n  "a": 2\n}'

    assert diet.compact_json(text) == '{"a":1,"a":2}'


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "{unquoted: 1}",
        '{"unterminated": "string',
        "[1, 2,",
        "<html><body>hello</body></html>",
    ],
)
def test_text_that_is_not_json_passes_through_untouched(text: str) -> None:
    assert diet.compact_json(text) == text


def test_trailing_whitespace_never_changes_the_line_count() -> None:
    text = "one   \n\n\n\ntwo\t\t\nthree  "

    stripped = diet.strip_trailing_whitespace(text)

    assert stripped == "one\n\n\n\ntwo\nthree"
    assert stripped.count("\n") == text.count("\n")


def test_default_diet_preserves_a_markdown_hard_break() -> None:
    text = "keep this hard break  \n" + "x" * diet.MIN_CANDIDATE_CHARS

    assert diet.DEFAULT_TRANSFORMS == (diet.JSON_COMPACTION,)
    assert diet.shrink(text) == (text, ())
    assert diet.shrink(text, (diet.TRAILING_WHITESPACE,)) == (
        "keep this hard break\n" + "x" * diet.MIN_CANDIDATE_CHARS,
        (diet.TRAILING_WHITESPACE,),
    )


def test_a_short_leaf_is_left_alone() -> None:
    text = '{"a": 1}'

    assert len(text) < diet.MIN_CANDIDATE_CHARS
    assert diet.shrink(text) == (text, ())


def test_shrink_reports_only_the_transforms_that_helped() -> None:
    shrunk, applied = diet.shrink(PRETTY_JSON)

    assert applied == (diet.JSON_COMPACTION,)
    assert len(shrunk) < len(PRETTY_JSON)

    text = "a line with trailing space   \n" + "b" * 80
    _shrunk, applied = diet.shrink(text, (diet.TRAILING_WHITESPACE,))
    assert applied == (diet.TRAILING_WHITESPACE,)


def test_disabling_a_transform_disables_exactly_that_transform() -> None:
    shrunk, applied = diet.shrink(PRETTY_JSON, (diet.TRAILING_WHITESPACE,))

    assert applied == ()
    assert shrunk == PRETTY_JSON
