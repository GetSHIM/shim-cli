from __future__ import annotations

import base64
import binascii
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from dataclasses import replace as evolve
from html import unescape
from itertools import islice
from typing import Any, Literal

from shim_cli.guard.entities import PLACEHOLDER
from shim_cli.guard.evaluate import FLOORS, issued, line_breaks, numbering_for
from shim_cli.guard.models import Finding

# Matches the hook's own MAX_INPUT_BYTES: anything that got past that refusal
# is worth scanning. Below this, a single large `Read` result was skipped here
# before the detector ever saw it, and reached the model unmasked.
# ponytail: the bound is size, not time. The per-call analysis deadline is the
# backstop; a shared deadline across pieces is the upgrade if this ever bites.
MAX_TEXT_CHARACTERS = 1_000_000
# Below the interpreter's recursion limit with room to spare. The hook has no
# leaf count: its 1 MB input bound already bounds the work, and a count below
# that only turned the fields after it into fields nobody inspected. Anything
# nested deeper than this is not inspected, so the hook withholds it.
MAX_DEPTH = 64
# How much of a value is read with its key in front. The detector's cue
# windows are 48 characters at most, so a key is context for the start of its
# value only; as with the detector's own pieces, a finding the key starts and
# that runs past this is still read whole.
KEY_CONTEXT = 4_096
# Enough base64 for a JPEG's frame header behind its metadata.
IMAGE_HEADER_CHARACTERS = 65_536

Path = tuple


class PayloadTooLarge(ValueError):
    pass


@dataclass
class Traversal:
    leaves: list = field(default_factory=list)
    characters: int = 0
    partial: bool = False
    skipped: int = 0
    reasons: set[str] = field(default_factory=set)
    max_leaves: int | None = None
    max_characters: int = MAX_TEXT_CHARACTERS
    max_depth: int = MAX_DEPTH

    def add(self, path: Path, text: str) -> None:
        if self.partial and self.characters + len(text) > self.max_characters:
            self.skipped += 1
            self.reasons.add("size-limit")
            return
        self.leaves.append((path, text))
        self.characters += len(text)
        if self.max_leaves is not None and len(self.leaves) > self.max_leaves:
            raise PayloadTooLarge("payload has too many text fields to scan safely")
        if self.characters > self.max_characters:
            raise PayloadTooLarge("payload text exceeds the safe analysis limit")


def walk(
    value: Any,
    root: Path = (),
    *,
    partial: bool = False,
    max_leaves: int | None = None,
    max_characters: int = MAX_TEXT_CHARACTERS,
    max_depth: int = MAX_DEPTH,
) -> Traversal:
    """The defaults bound a hook a user is waiting on; only the proxy changes them."""
    found = Traversal(
        partial=partial,
        max_leaves=max_leaves,
        max_characters=max_characters,
        max_depth=max_depth,
    )
    _walk(value, root, found, 0)
    return found


def _walk(value: Any, path: Path, found: Traversal, depth: int) -> None:
    """Keys, text and numbers are leaves. A key's leaf is `(key,)` after the
    path of its object."""
    if depth > found.max_depth:
        if found.partial:
            found.skipped += 1
            found.reasons.add("depth-limit")
            return
        raise PayloadTooLarge("payload is nested more deeply than is safe to scan")
    if isinstance(value, str):
        if value:
            found.add(path, value)
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and value.is_integer():
            value = int(value)  # 9.05e11 is 905321234567, not "905321234567.0"
        found.add(path, str(value))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            if key:
                found.add((*path, (key,)), key)
            _walk(item, (*path, key), found, depth + 1)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, (*path, index), found, depth + 1)


def replace(value: Any, replacements: dict) -> Any:
    return _replace(value, (), replacements)


def _replace(value: Any, path: Path, replacements: dict) -> Any:
    if len(path) > MAX_DEPTH:
        return value
    if isinstance(value, dict):
        rebuilt: dict = {}
        for key, item in value.items():
            if isinstance(key, str):
                item = _replace(item, (*path, key), replacements)
                key = replacements.get((*path, (key,)), key)
            if key in rebuilt:
                # Two keys masked to one placeholder: neither can be kept.
                raise ValueError("masked keys collide")
            rebuilt[key] = item
        return rebuilt
    if isinstance(value, list):
        return [
            _replace(item, (*path, index), replacements)
            for index, item in enumerate(value)
        ]
    replacement = replacements.get(path)
    if replacement is None:
        return value
    if not isinstance(replacement, str):
        raise TypeError("a leaf may only be replaced by a string")
    return replacement


def past_deadline(error: BaseException) -> bool:
    # The detector wraps its own timeout in a ValueError; the hook's is bare.
    return isinstance(error, TimeoutError) or isinstance(error.__cause__, TimeoutError)


def _shrink(
    text: str, decision, transforms: tuple, shrink, evaluate, numbering
) -> tuple:
    """What the model will read of `text`, the transforms that made it, and
    what was found in it. Raises only past the deadline.

    A page is converted first and scanned as the model will read it. If that
    cannot be done, the markup is masked and handed on unconverted.
    """
    if "html" in transforms and not decision.partial:
        page = _page(text, decision, shrink, evaluate, numbering)
        if page is not None:
            return page
    kept = tuple(name for name in transforms if name != "html")
    shrunk, names = shrink(_masked(text, decision), kept)
    return shrunk, names, decision


# Conversion keeps private-use characters as text. Three the page holds
# neither as written nor through an entity, put around each value found in
# the markup with its index, show where it lands on the page.
_PRIVATE_USE = range(0xE000, 0xF900)


def _marks(text: str) -> tuple | None:
    held = set(text) | set(unescape(text))
    free = (chr(code) for code in _PRIVATE_USE if chr(code) not in held)
    marks = tuple(islice(free, 3))
    return marks if len(marks) == 3 else None


def _page(text: str, found, shrink, evaluate, numbering: dict) -> tuple | None:
    """The page `text` converts to, masked, or None if it must be masked as
    markup. Raises past the deadline.

    The page is scanned as the model will read it: entities decode, and a
    value cut in two by tags is read whole. Conversion also joins a value to
    the text beside it, where no recognizer finds it, so a value `found` in
    the markup is masked where it lands too, with the placeholder it was
    given there. One that runs across tags has no one place on the page: it
    is masked in the markup, and lands as its placeholder.
    """
    pieces, landed, cursor = [], [], 0
    given = _placeholders(text, found)
    marks = _marks(text) if found.findings else ("", "", "")
    if given is None or marks is None:
        return None
    opening, split, closing = marks
    for index, finding in enumerate(found.findings):
        value = text[finding.start : finding.end]
        after = ""
        if "<" in value or ">" in value:
            value, after = given[index], line_breaks(value)
        pieces += (
            text[cursor : finding.start],
            f"{opening}{index}{split}{value}{closing}{after}",
        )
        cursor = finding.end
    marked, names = shrink("".join(pieces) + text[cursor:], ("html",))
    if not names:
        return None
    page = marked
    if found.findings:
        parts, length, cursor = [], 0, 0
        for match in re.finditer(
            f"{opening}([0-9]+){split}([^{opening}{split}{closing}]*){closing}", marked
        ):
            before = marked[cursor : match.start()]
            start = length + len(before)
            length = start + len(match.group(2))
            parts += (before, match.group(2))
            landed.append((int(match.group(1)), start, length))
            cursor = match.end()
        page = "".join(parts) + marked[cursor:]
        if any(mark in page for mark in marks):
            return None  # a mark without its pair
    # A placeholder the page shows that the numbers did not start above, such
    # as one a tag splits in the markup, would be shim's own: keep the markup.
    text_shown = list(page)
    for _index, start, end in landed:
        text_shown[start:end] = " " * (end - start)
    floors = numbering[FLOORS]
    if any(
        top > floors.get(kind, 0)
        for kind, top in numbering_for(("".join(text_shown),))[FLOORS].items()
    ):
        return None
    try:
        read = evaluate(page, numbering=numbering)
    except (ValueError, TimeoutError) as error:
        if past_deadline(error):
            raise
        return None
    if read.partial:
        return None
    covered = bytearray(len(page))
    for finding in read.findings:
        covered[finding.start : finding.end] = b"\x01" * (finding.end - finding.start)
    joined = [
        (index, start, end) for index, start, end in landed if 0 in covered[start:end]
    ]
    if not joined:
        return _masked(page, read), names, read
    shown = _placeholders(page, read)
    if shown is None:
        return None
    masked, findings = _merged(
        page,
        [
            (item.start, item.end, item, shown[at])
            for at, item in enumerate(read.findings)
        ]
        + [(start, end, found.findings[at], given[at]) for at, start, end in joined],
    )
    return masked, names, evolve(read, findings=findings, redacted_text=masked)


def _merged(text: str, regions: list) -> tuple:
    """`text` with each (start, end, finding, placeholder) region masked, and
    the findings that masked it. Overlapping values are masked as one, named
    by the first."""
    merged: list = []
    for region in sorted(regions, key=lambda region: (region[0], -region[1])):
        if merged and region[0] < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], region[1]), *merged[-1][2:])
        else:
            merged.append(region)
    pieces, findings, cursor = [], [], 0
    for start, end, item, placeholder in merged:
        pieces += (text[cursor:start], placeholder, line_breaks(text[start:end]))
        findings.append(Finding(item.entity_type, start, end, item.score, item.label))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces), tuple(findings)


def _placeholders(text: str, decision) -> list | None:
    """The placeholder `decision` wrote for each of its findings, in order,
    or None if it wrote them otherwise: each replaced its value, followed by
    the value's line breaks."""
    written, shift = [], 0
    for finding in decision.findings:
        match = PLACEHOLDER.match(decision.redacted_text, finding.start + shift)
        if match is None:
            return None
        written.append(match.group())
        span = text[finding.start : finding.end]
        shift += len(match.group()) + len(line_breaks(span)) - len(span)
    return written


def _masked(text: str, decision) -> str:
    return decision.redacted_text if decision.findings else text


def _node(value: Any, path: Path) -> Any:
    for step in path:
        value = value[step]
    return value


def _key(value: Any, path: Path) -> str | None:
    """The key a value is read under: its own or, in an entry such as
    `{"name": "DB_PASSWORD", "value": ...}`, its sibling's. A key, a list item
    and the whole event are read alone."""
    if not path or not isinstance(path[-1], str):
        return None
    if path[-1] in ("value", "Value"):
        entry = _node(value, path[:-1])
        for name in ("name", "Name", "key", "Key"):
            if isinstance(entry.get(name), str) and entry[name]:
                return entry[name]
    return path[-1]


def _dimensions(raw: bytes) -> tuple[int, int] | None:
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
        return struct.unpack(">II", raw[16:24])
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", raw[6:10])
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        chunk = raw[12:16]
        if chunk == b"VP8 " and raw[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", raw[26:30])
            return width & 0x3FFF, height & 0x3FFF
        if chunk == b"VP8L" and raw[20:21] == b"\x2f":
            bits = int.from_bytes(raw[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8X":
            width = int.from_bytes(raw[24:27], "little") + 1
            return width, int.from_bytes(raw[27:30], "little") + 1
        return None
    if raw[:2] != b"\xff\xd8":
        return None
    position = 2
    while position + 4 <= len(raw):
        if raw[position] != 0xFF:
            return None
        marker = raw[position + 1]
        if marker == 0xFF:
            position += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            position += 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", raw[position + 5 : position + 9])
            return width, height
        position += 2 + struct.unpack(">H", raw[position + 2 : position + 4])[0]
    return None


def image_size(data: str) -> tuple[int, int] | None:
    """Width and height from the header alone; None when it cannot be read."""
    head = data[:IMAGE_HEADER_CHARACTERS]
    try:
        raw = base64.b64decode(head[: len(head) - len(head) % 4], validate=True)
        size = _dimensions(raw)
    except (binascii.Error, ValueError, struct.error):
        return None
    return size if size and size[0] > 0 and size[1] > 0 else None


def _picture(data: str) -> bool:
    """A real image: a readable header, and base64 to the last character,
    so no text rides behind the part the header check reads."""
    try:
        base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return False
    return image_size(data) is not None


def _type(value: Any, path: Path) -> Any:
    block = _node(value, path)
    return block.get("type") if isinstance(block, dict) else None


def _opaque(value: Any, path: Path) -> str | None:
    """ "image" or "pdf" when `path` holds a file's bytes: a Read's
    `file.base64` or page, or an image block's `data`. No placeholder can
    stand in for them without breaking the file."""
    if path[-2:] == ("file", "base64"):
        kind = _type(value, path[:-2])
        return kind if kind in ("image", "pdf") else None
    if path[-3:-2] == ("pages",) and path[-1:] == ("base64",):
        return "image" if _type(value, path[:-3]) == "parts" else None
    if path[-1:] == ("data",) and _type(value, path[:-1]) == "image":
        return "image"
    return None


def _read(text: str, key: str | None, read, evaluate):
    """The detector's decision on `text`, read alone and, under `key`, as
    `"key": "text"` too, so a key that names a secret or a phone is its
    value's context. Each reading keeps what it found in the value: the
    context only adds, and a value that starts the text alone is still
    read as one. `read` gives the event's numbering; a long value's start is
    read with its key against none, as its findings only decide."""
    alone = read(text)
    if key is None:
        return alone
    prefix = f'"{key}": "'
    start = len(prefix)
    if len(text) > KEY_CONTEXT:
        head = evaluate(f'{prefix}{text[:KEY_CONTEXT]}"', numbering={})
        cut = start + KEY_CONTEXT
        spans = {(item.start + start, item.end + start) for item in alone.findings}
        if all(
            item.end <= start or ((item.start, item.end) in spans and item.end < cut)
            for item in head.findings
        ):
            return alone  # the key added nothing at the start
    framed = f'{prefix}{text}"'
    near = read(framed)
    end = start + len(text)
    kept = [
        (at, item)
        for at, item in enumerate(near.findings)
        if item.end > start and item.start < end
    ]
    # A number named by its key is no longer bare; the key's own are not
    # the value's.
    bare = min(alone.bare_numbers, near.bare_numbers)
    if not kept:
        return evolve(alone, bare_numbers=bare)
    own, shown = _placeholders(text, alone), _placeholders(framed, near)
    if own is None or shown is None:
        raise ValueError("a placeholder could not be placed in the value")
    masked, findings = _merged(
        text,
        [
            (item.start, item.end, item, own[at])
            for at, item in enumerate(alone.findings)
        ]
        + [
            (
                max(item.start, start) - start,
                min(item.end, end) - start,
                item,
                shown[at],
            )
            for at, item in kept
        ],
    )
    return evolve(
        alone,
        findings=findings,
        redacted_text=masked,
        partial=alone.partial or near.partial,
        bare_numbers=bare,
    )


@dataclass(frozen=True)
class Inspection:
    value: Any
    findings: list
    changed: bool
    transforms: tuple
    markers: tuple
    status: Literal["complete", "partial", "not-inspected"] = "complete"
    skipped: int = 0
    reasons: tuple[str, ...] = ()
    bare_numbers: int = 0
    # Something was found where no placeholder can stand in for it: a file's
    # bytes, or two keys that would become one. `value` is then None.
    unmaskable: bool = False
    # (type, first, last) of the placeholder numbers given, and whether what
    # the model reads of the event already holds placeholder-shaped text.
    issued: tuple = ()
    literal: bool = False


def inspect(
    value: Any,
    evaluate: Callable[..., Any],
    transforms: tuple = (),
    inbound: bool = False,
) -> Inspection:
    """`value` is one event. `evaluate(text, numbering=...)` is the detector,
    given one `numbering` for every field of it. An `inbound` event is a
    result the model will read: it is scanned for markers, and an image in it
    reaches the model as a picture, so its bytes are not read as text."""
    found = walk(value, partial=True)
    transform_order = ()
    apply_diet = None
    if transforms:
        from .diet import TRANSFORMS, shrink

        transform_order = TRANSFORMS
        apply_diet = shrink
    marker_order = ()
    scan_injection = None
    if inbound:
        from .injection import MARKERS, scan

        marker_order = MARKERS
        scan_injection = scan
    replacements = {}
    findings = []
    applied: set = set()
    markers: set = set()
    inspected = 0
    bare_numbers = 0
    unmaskable = False
    # One numbering for the whole event, so a value keeps its placeholder in
    # every field. Held here only, as are the decisions: both map values.
    # Numbers start above any placeholder the model will read as text here:
    # in a field as written, or on a page as converted, where an entity such
    # as &lt;EMAIL_1&gt; decodes to one. `_page` keeps a page that shows one
    # some other way as markup.
    shown = [text for _path, text in found.leaves]
    if "html" in transforms:
        shown += [unescape(text) for text in shown]
    numbering = numbering_for(shown)
    # A text read once is read for every field that repeats it.
    decisions: dict = {}

    def read(text: str):
        if text not in decisions:
            decisions[text] = evaluate(text, numbering=numbering)
        return decisions[text]

    for index, (path, text) in enumerate(found.leaves):
        named = bool(path) and isinstance(path[-1], tuple)
        opaque = None if named else _opaque(value, path)
        if inbound and opaque == "image" and _picture(text):
            continue  # a picture to the model, never text
        try:
            # A file's bytes are read alone: their key adds nothing.
            key = None if named or opaque else _key(value, path)
            decision = _read(text, key, read, evaluate)
        except (ValueError, TimeoutError) as error:
            # Do not consume the hook deadline and continue.
            if past_deadline(error):
                found.skipped += len(found.leaves) - index
                found.reasons.add("deadline")
                break
            found.skipped += 1
            found.reasons.add("analysis-failed")
            continue
        inspected += 1
        bare_numbers += decision.bare_numbers
        if decision.partial:
            # A piece of an oversized leaf failed; the rest is masked below.
            found.skipped += 1
            found.reasons.add("piece-failed")
        if opaque and decision.findings:
            unmaskable = True
            findings.append((path, decision))
            continue
        current = decision.redacted_text if decision.findings else text
        late = False
        # Only text is reshaped: a key, a number or a file's bytes are not.
        if (
            apply_diet is not None
            and not (named or opaque)
            and isinstance(_node(value, path), str)
        ):
            try:
                current, names, decision = _shrink(
                    text, decision, transforms, apply_diet, evaluate, numbering
                )
                applied.update(names)
            except (ValueError, TimeoutError):
                late = True  # past the deadline: this leaf is masked, not shrunk
        if scan_injection is not None:  # what the model will read
            markers.update(scan_injection(current))
        if decision.findings:
            findings.append((path, decision))
        if current != text:
            replacements[path] = current
        if late:
            found.skipped += len(found.leaves) - index - 1
            found.reasons.add("deadline")
            break
    ordered_transforms = tuple(name for name in transform_order if name in applied)
    ordered_markers = tuple(name for name in marker_order if name in markers)
    rewritten = value
    if replacements and not found.skipped and not unmaskable:
        try:
            rewritten = replace(value, replacements)
        except ValueError:  # two keys masked to one name: neither can stay
            unmaskable = True
    status = "complete"
    if found.skipped:
        status = "partial" if inspected else "not-inspected"
    if found.skipped or unmaskable:
        # Part of it was never read, or cannot be masked: no version of it
        # may be handed on.
        rewritten, replacements = None, {}
    return Inspection(
        rewritten,
        findings,
        bool(replacements),
        ordered_transforms,
        ordered_markers,
        status,
        found.skipped,
        tuple(sorted(found.reasons)),
        bare_numbers,
        unmaskable,
        issued(numbering),
        any(PLACEHOLDER.search(text) for text in shown),
    )


def mask(value: Any, evaluate: Callable[..., Any]) -> tuple:
    result = inspect(value, evaluate)
    return result.value, result.findings, result.changed
