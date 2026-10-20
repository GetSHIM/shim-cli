"""Claude Code's `@` mentions are read in time linear in the prompt, and
exactly as the client's parser reads them."""

from __future__ import annotations

import importlib
import random
import re
import time
from pathlib import Path

import pytest

from shim_cli.clients.claude.hook import mentions

_START = r"(?:^|(?<=[\s。、？！]))@"
_REFERENCE_QUOTED = re.compile(_START + r'"([^"]+)"')
_REFERENCE_BARE = re.compile(
    _START
    + r"(\S+)(?:(?<=[A-Za-z0-9_])(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])(?=[A-Za-z0-9_]))"
)
_PARTS = ("@", "@", "@", " ", "\n", "。", "、", "？", "！", "　", '"', '"')
_PARTS += ("a", "Z", "9", "_", "-", ".", "/", ":", "#", "ğ", "日", "(", " (agent)")


def _reference(prompt: str) -> tuple[str, ...]:
    """The two regular expressions `mentions` used through 1.2.2."""
    quoted = [
        match.group(1)
        for match in _REFERENCE_QUOTED.finditer(prompt)
        if not match.group(1).endswith(" (agent)")
    ]
    bare = [
        match.group(1)
        for match in _REFERENCE_BARE.finditer(prompt)
        if not match.group(1).startswith('"')
    ]
    return tuple(
        dict.fromkeys(
            mention for mention in quoted + bare if ":" not in mention.split("/", 1)[0]
        )
    )


def test_mentions_are_exactly_what_the_client_s_expressions_read() -> None:
    pick = random.Random(25)
    prompts = [
        "".join(pick.choice(_PARTS) for _ in range(pick.randrange(1, 40)))
        for _ in range(60_000)
    ]

    for prompt in prompts:
        assert mentions(prompt) == _reference(prompt), prompt


@pytest.mark.parametrize(
    "unit",
    ["。@", "、@-", '！@"', "？@a", " @。", "@" + "-" * 999 + " ", ' @"a'],
    ids=[
        "ideographic-full-stop",
        "dash",
        "quote",
        "word",
        "space",
        "long-run",
        "open-quote",
    ],
)
def test_a_megabyte_prompt_of_mentions_is_read_well_inside_the_deadline(
    unit: str,
) -> None:
    """Each `@` of a run such as `。@。@...` used to be tried against the rest
    of the run: 20,000 characters took 4 s and the 1 MB the hook accepts would
    take hours, in one call the hook's alarm cannot interrupt."""
    hook = importlib.import_module("shim_cli.hook")
    prompt = unit * (1_000_000 // len(unit.encode()))

    started = time.process_time()
    mentions(prompt)
    spent = time.process_time() - started

    assert spent < hook.HOOK_DEADLINE_SECONDS / 20


def test_one_file_named_many_ways_is_read_and_reported_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`@notes.txt#1 @notes.txt#2 ...` names one attachment: each mention used
    to read the file, run the detector on its name and write one ledger
    record, and the message listed `@notes.txt` 50,000 times."""
    claude = importlib.import_module("shim_cli.clients.claude.hook")
    (tmp_path / "notes.txt").write_text("key AKIAIOSFODNN7EXAMPLE\n")
    reads: list[str] = []
    real_read = claude._read
    monkeypatch.setattr(
        claude,
        "_read",
        lambda path, limit: reads.append(path) or real_read(path, limit),
    )
    prompt = " ".join(f"@notes.txt#{index}" for index in range(50_000))

    found = list(claude.attached(prompt, str(tmp_path)))

    assert found == [("notes.txt", "key AKIAIOSFODNN7EXAMPLE\n")]
    assert len(reads) == 1


def test_a_file_past_the_count_limit_is_named_once_and_read_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Past `MAX_ATTACHMENTS` nothing is inspected, so the lines a mention
    picks do not matter: each file past the limit is read once, to see the
    client would attach it, and named once."""
    claude = importlib.import_module("shim_cli.clients.claude.hook")
    names = [f"f{index}.txt" for index in range(claude.MAX_ATTACHMENTS)]
    for name in [*names, "late.txt"]:
        (tmp_path / name).write_text("one\ntwo\n")
    reads: list[str] = []
    real_read = claude._read
    monkeypatch.setattr(
        claude,
        "_read",
        lambda path, limit: reads.append(path) or real_read(path, limit),
    )
    prompt = " ".join(
        [f"@{name}" for name in names]
        + [f"@late.txt#L{index}" for index in range(1, 1_000)]
        + ["@f0.txt#L2"]
    )

    found = list(claude.attached(prompt, str(tmp_path)))

    assert found == [(name, "one\ntwo\n") for name in names] + [
        ("late.txt", None),
        ("f0.txt", None),
    ]
    assert len(reads) == len(names) + 2
