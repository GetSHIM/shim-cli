from __future__ import annotations

import os
from pathlib import Path

import pytest

from shim_cli.clients.claude.hook import (
    MAX_ATTACHED_BYTES,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    attached,
    mentions,
)
from shim_cli.clients.user_prompt_hook import (
    MAX_OUTPUT_BYTES,
    block_output,
    warn_output,
)
from shim_cli.guard.entities import ENTITY_TYPES
from shim_cli.guard.models import Finding, GuardDecision


@pytest.mark.parametrize(
    ("prompt", "expected"),
    (
        ("@.env explain", (".env",)),
        ("explain @config/settings.py.", ("config/settings.py",)),
        ("see (@a.txt), then @b.txt!", ("b.txt",)),
        ("mail a@b.com now", ()),
        ("@server:resource and @db:x/y", ()),
        ('@"a b.txt" @notes.txt#L2-3', ("a b.txt", "notes.txt#L2-3")),
        ("前文。@日本.txt を見て", ("日本.txt",)),
        ('@"reviewer (agent)" please', ()),
        ("@.env @.env", (".env",)),
        ("@src/ list", ("src",)),
        ("@~/notes.txt and @/etc/hosts", ("~/notes.txt", "/etc/hosts")),
        ("tab\t@x.txt", ("x.txt",)),
        ("nbsp @y.txt", ("y.txt",)),
        ("ideographic　@z.txt", ("z.txt",)),
        ("@notlar.ğ", ("notlar",)),
        ("@agent-reviewer go", ("agent-reviewer",)),
        ("no mention here", ()),
    ),
)
def test_mentions_match_the_client_parser(prompt: str, expected: tuple) -> None:
    assert mentions(prompt) == expected


def _write(directory: Path, name: str, content: bytes | str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


def test_an_attached_file_is_read_relative_to_cwd(tmp_path: Path) -> None:
    _write(tmp_path, "config/settings.py", "EMAIL = 'ops@example.com'\n")

    assert list(attached("look at @config/settings.py", str(tmp_path))) == [
        ("config/settings.py", "EMAIL = 'ops@example.com'\n")
    ]


@pytest.mark.parametrize(
    "prompt", ("@src", "@missing.txt", "@pipe", "@binary.bin", "@a\x00b")
)
def test_what_is_not_a_readable_text_file_is_skipped_silently(
    prompt: str, tmp_path: Path
) -> None:
    (tmp_path / "src").mkdir()
    os.mkfifo(tmp_path / "pipe")
    _write(tmp_path, "binary.bin", b"\xff\xfe\x00")

    assert list(attached(prompt, str(tmp_path))) == []


def test_a_file_the_client_would_not_attach_is_skipped_silently(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "edge.txt", "a" * MAX_ATTACHMENT_BYTES)
    _write(tmp_path, "over.txt", "a" * (MAX_ATTACHMENT_BYTES + 1))
    _write(tmp_path, "shot.png", b"\x89PNG" + b"\x00" * 1_500_000)

    [(edge, text)] = attached("@edge.txt", str(tmp_path))

    assert MAX_ATTACHMENT_BYTES == 262_144
    assert (edge, len(text or "")) == ("edge.txt", MAX_ATTACHMENT_BYTES)
    assert list(attached("@over.txt @shot.png", str(tmp_path))) == []


def test_the_attachments_of_one_prompt_share_one_budget(tmp_path: Path) -> None:
    names = [f"part{index}.log" for index in range(5)]
    for name in names:
        _write(tmp_path, name, "a" * 250_000)

    found = list(attached(" ".join(f"@{name}" for name in names), str(tmp_path)))

    assert MAX_ATTACHED_BYTES == 1_000_000
    assert [text is None for _name, text in found] == [False] * 4 + [True]


def test_text_that_is_not_utf8_is_read_the_way_the_client_reads_it(
    tmp_path: Path,
) -> None:
    content = "# Türkçe ayarlar\nAPI_TOKEN=0123456789abcdef\n"
    _write(tmp_path, "latin.env", content.encode("iso-8859-9"))

    assert list(attached("@latin.env", str(tmp_path))) == [
        ("latin.env", "# T\ufffdrk\ufffde ayarlar\nAPI_TOKEN=0123456789abcdef\n")
    ]


def test_files_past_the_count_limit_are_not_read(tmp_path: Path) -> None:
    names = [f"f{index}.txt" for index in range(MAX_ATTACHMENTS + 1)]
    for name in names:
        _write(tmp_path, name, "x")

    found = list(attached(" ".join(f"@{name}" for name in names), str(tmp_path)))

    assert [name for name, _text in found] == names
    assert [text for _name, text in found] == ["x"] * MAX_ATTACHMENTS + [None]


@pytest.mark.parametrize(
    ("mention", "expected"),
    (
        ("@notes.txt#L2-3", "two\nthree"),
        ("@notes.txt#L4", "four"),
        ("@notes.txt#L0", "one\ntwo\nthree\nfour\n"),
        ("@notes.txt#L0-2", "one\ntwo\nthree\nfour\n"),
        ("@notes.txt#L3-0", "three\nfour\n"),
        ("@notes.txt#L3-2", ""),
    ),
)
def test_a_line_range_reads_what_the_client_attaches(
    mention: str, expected: str, tmp_path: Path
) -> None:
    _write(tmp_path, "notes.txt", "one\ntwo\nthree\nfour\n")

    assert list(attached(mention, str(tmp_path))) == [("notes.txt", expected)]


def test_home_and_absolute_paths_resolve_as_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    _write(home, "notes.txt", "home")
    other = _write(tmp_path / "elsewhere", "absolute.txt", "absolute")
    monkeypatch.setenv("HOME", str(home))

    found = list(attached(f"@~/notes.txt @{other}", str(tmp_path / "cwd")))

    assert found == [("~/notes.txt", "home"), (str(other), "absolute")]


def test_an_unknown_user_home_is_skipped(tmp_path: Path) -> None:
    assert list(attached("@~nosuchuser0000/x.txt", str(tmp_path))) == []


@pytest.mark.parametrize("cwd", (None, 7, "relative/dir", ""))
def test_without_an_absolute_cwd_nothing_is_read(
    cwd: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, ".env", "TOKEN=0123456789abcdef")

    assert list(attached("@.env", cwd)) == []


def test_the_longest_attachment_report_fits_the_output_limit() -> None:
    findings = tuple(
        Finding(entity, index, index + 1, 1.0, "")
        for index, entity in enumerate(ENTITY_TYPES)
    )
    decision = GuardDecision(findings, "x", False, 0)
    name = "…" + "\U0001f600" * 120
    held = tuple(
        (name, tuple((entity, 99_999) for entity in ENTITY_TYPES)) for _ in range(9)
    )
    unread = (name,) * 9

    assert len(warn_output(decision, held, unread)) <= MAX_OUTPUT_BYTES
    assert (
        len(
            block_output(decision, "/tmp/shim-redacted-0123456789abcdef.txt", held=held)
        )
        <= MAX_OUTPUT_BYTES
    )
