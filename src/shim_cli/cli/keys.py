from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import typer

from shim_cli.cli.output import emit_error, emit_json

MAX_FILE_BYTES = 1_000_000
_LINE_END = re.compile(r"\r\n|\r|\n")
_SECTION = re.compile(r"\[([^\]]+)\]")
_ASSIGNMENT = re.compile(
    r"(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)\s*=(?P<rest>.*)"
)
_REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_COMMENT = re.compile(r"\s#")
_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]+-----")
_KEY_END = re.compile(r"-----END [A-Z0-9 ]+-----")
_KEY_BODY = re.compile(r"[A-Za-z0-9+/]+={0,2}\\?|=[A-Za-z0-9+/]+|[A-Za-z-]+: .*|")
_KEY_MATERIAL = re.compile(r"(?=.*\d)(?=.*[a-z])(?=.*[A-Z])[A-Za-z0-9_-]{8,}")
_CLOSING = {
    '"': re.compile(r'((?:[^"\\]|\\.)*)"', re.DOTALL),
    "'": re.compile(r"([^']*)'"),
    "`": re.compile(r"([^`]*)`"),
}


class Entry(NamedTuple):
    kind: str
    name: str
    value: str
    line: int
    section: str
    literal: bool = False


def _opens_a_key(line: str) -> bool:
    begins = [found.end() for found in _KEY_BEGIN.finditer(line)]
    return bool(begins) and not _KEY_END.search(line, begins[-1])


def _rest_of_key(text: str, value: str, position: int) -> tuple[str, int, int]:
    lines = 0
    while position < len(text):
        end = _LINE_END.search(text, position)
        line = text[position : end.start() if end else len(text)].strip()
        if not line.startswith(("-----BEGIN", "-----END")) and not _KEY_BODY.fullmatch(
            line
        ):
            break
        value, lines = f"{value}\n{line}", lines + 1
        position = end.end() if end else len(text)
        if line.startswith("-----END") and not _opens_a_key(line):
            break
    return value, position, lines


def parse(text: str) -> list[Entry]:
    entries: list[Entry] = []
    section = ""
    ini = False
    position = number = key_indent = 0
    while position < len(text):
        number += 1
        start = position
        end = _LINE_END.search(text, position)
        position = end.end() if end else len(text)
        raw = text[start : end.start() if end else len(text)]
        line = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        if not line:
            continue
        if line.startswith("#"):
            if _opens_a_key(line):
                _, position, lines = _rest_of_key(text, line, position)
                number += lines
            continue
        if ini and indent > key_indent and entries and entries[-1].kind == "variable":
            entries[-1] = entries[-1]._replace(value=f"{entries[-1].value}\n{line}")
            continue
        found = _SECTION.fullmatch(line)
        if found and found.group(1).isprintable():
            section = found.group(1)
            ini = True
            entries.append(Entry("section", section, "", number, section))
            continue
        found = _ASSIGNMENT.fullmatch(line)
        if found is None:
            entries.append(Entry("unparsed", "", "", number, section))
            if _opens_a_key(line):
                _, position, lines = _rest_of_key(text, line, position)
                number += lines
            continue
        if _KEY_MATERIAL.fullmatch(found.group("name")) and found.group(
            "rest"
        ).strip() in ("", "="):
            entries.append(Entry("unparsed", "", "", number, section))
            continue
        key_indent = indent
        name, value = found.group("name"), found.group("rest").lstrip()
        if value[:1] not in _CLOSING or value[:3] in ('"""', "'''"):
            value = _COMMENT.split(found.group("rest"), maxsplit=1)[0].strip()
            if _opens_a_key(value):
                value, position, lines = _rest_of_key(text, value, position)
                entries.append(Entry("variable", name, value, number, section))
                number += lines
                continue
            entries.append(Entry("variable", name, value, number, section))
            continue
        opening = start + len(raw) - len(raw.lstrip()) + found.end("rest") - len(value)
        closing = _CLOSING[value[0]].match(text, opening + 1)
        if closing is None:
            if _opens_a_key(value):
                value, position, lines = _rest_of_key(text, value, position)
                entries.append(Entry("variable", name, value, number, section))
                number += lines
            else:
                entries.append(Entry("unparsed", "", "", number, section))
            continue
        literal = value[0] != '"'
        entries.append(
            Entry("variable", name, closing.group(1), number, section, literal)
        )
        crossed = len(_LINE_END.findall(text, opening, closing.end()))
        if crossed:
            number += crossed
            end = _LINE_END.search(text, closing.end())
            position = end.end() if end else len(text)
        if _opens_a_key(closing.group(1)):
            _, position, lines = _rest_of_key(text, "", position)
            number += lines
    return entries


def _read(path: Path, as_json: bool) -> str:
    if not path.exists():
        emit_error(
            "keys",
            "FILE_NOT_FOUND",
            f"{path} does not exist",
            None,
            as_json=as_json,
            plain=True,
        )
    if not path.is_file():
        emit_error(
            "keys",
            "NOT_A_FILE",
            f"{path} is not a regular file",
            None,
            as_json=as_json,
            plain=True,
        )
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        emit_error(
            "keys",
            "FILE_TOO_LARGE",
            f"{path} is larger than 1 MB",
            None,
            as_json=as_json,
            plain=True,
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        emit_error(
            "keys",
            "NOT_UTF8",
            f"{path} is not UTF-8 text",
            None,
            as_json=as_json,
            plain=True,
        )


def _row(entry: Entry) -> dict:
    from shim_cli.cli.privacy import evaluate

    row = {
        "name": entry.name or None,
        "state": entry.kind,
        "entity": None,
        "line": entry.line,
        "section": entry.section,
        "reference": None,
    }
    if entry.kind != "variable":
        return row
    if not entry.value:
        row["state"] = "empty"
    elif not entry.literal and _REFERENCE.fullmatch(entry.value):
        row["state"], row["reference"] = "ref", entry.value
    else:
        row["state"] = "set"
        try:
            findings = evaluate(f"{entry.name}={entry.value}").findings
        except Exception:
            row["entity"] = "not inspected"
            return row
        types = dict.fromkeys(
            finding.entity_type
            for finding in findings
            if finding.start >= len(entry.name) + 1
        )
        row["entity"] = ", ".join(types) or None
    return row


def _shown_sections(entries: list[Entry]) -> list[Entry]:
    from shim_cli.cli.privacy import evaluate

    shown: dict[str, str] = {"": ""}
    for entry in entries:
        if entry.section not in shown:
            try:
                shown[entry.section] = evaluate(entry.section).redacted_text
            except Exception:
                shown[entry.section] = "…"
    return [
        entry._replace(
            section=shown[entry.section],
            name=shown[entry.section] if entry.kind == "section" else entry.name,
        )
        for entry in entries
    ]


def keys(paths: list[Path], *, as_json: bool) -> None:
    files = [(path, _shown_sections(parse(_read(path, as_json)))) for path in paths]
    if as_json:
        emit_json(
            "keys",
            "ok",
            files=[
                {
                    "path": str(path),
                    "variables": [
                        _row(entry) for entry in entries if entry.kind != "section"
                    ],
                }
                for path, entries in files
            ],
        )
        return
    blocks = []
    for path, entries in files:
        lines = [str(path)]
        for entry in entries:
            if entry.kind == "section":
                lines.append(f"  [{entry.name}]")
            elif entry.kind == "unparsed":
                lines.append(f"  unparsed line {entry.line}")
            else:
                row = _row(entry)
                shown = f"({row['reference']})" if row["reference"] else row["entity"]
                lines.append(
                    f"  {entry.name:<23} {row['state']:<5} {shown or ''}".rstrip()
                )
        blocks.append("\n".join(lines))
    typer.echo("\n\n".join(blocks))
