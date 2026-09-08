from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field

from ..events.payload import PayloadTooLarge, walk

SECTIONS = ("tools", "system", "messages")
OTHER = "other"
MEMOISED = ("tools", "system")
MEMO_LIMIT = 16
# The hook's limits bound a user waiting on a deadline. Measurement runs after
# the response is relayed, so the body cap above is the bound that counts. One
# measured Claude Code request: 4,402 leaves, 256,517 characters, 35 deep — the
# depth is an MCP tool's recursive JSON schema, and the hook's 24 stops there.
MAX_SCAN_LEAVES = 50_000
MAX_SCAN_DEPTH = 200

MAX_BODY_BYTES = 8_000_000

# Why a request could not be measured. The report turns these into sentences,
# so each one has to name a cause the person can act on.
SLOTS_BUSY = "slots busy"
BODY_TOO_LARGE = "body too large"
NOT_JSON = "not JSON"
TOO_MANY_FIELDS = "too many fields"
INCOMPLETE_REASONS = (SLOTS_BUSY, BODY_TOO_LARGE, NOT_JSON, TOO_MANY_FIELDS)

MAX_MODEL_CHARS = 120
UNKNOWN_MODEL = "unknown"

MAX_RESPONSE_CHARACTERS = 1_000_000
RESPONSE_KINDS = ("text", "thinking")
_DELTA_KINDS = {"text_delta": "text", "thinking_delta": "thinking"}

MAX_STOP_REASON_CHARS = 40
UNKNOWN_REASON = "unknown"
# Every provider spelling for "the answer stopped at the output limit".
TRUNCATED = frozenset({"max_tokens", "max_output_tokens", "length"})

AT_FILE_MARKER = "Called the Read tool with the following input:"
_REMINDER_OPEN = "<system-reminder>"
_REMINDER_CLOSE = "</system-reminder>"


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_input(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    def merge(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens or other.input_tokens,
            output_tokens=max(self.output_tokens, other.output_tokens),
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens or other.cache_creation_input_tokens
            ),
            cache_read_input_tokens=(
                self.cache_read_input_tokens or other.cache_read_input_tokens
            ),
        )


def _reason(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    printable = len(value) <= MAX_STOP_REASON_CHARS and value.isprintable()
    return value if printable else UNKNOWN_REASON


def _incomplete(value: object) -> str:
    """The Responses shape states truncation as a status, not a reason."""
    if not isinstance(value, dict) or value.get("status") != "incomplete":
        return ""
    details = value.get("incomplete_details")
    return _reason(details.get("reason") if isinstance(details, dict) else None)


def stop_reason_from(document: object) -> str:
    if not isinstance(document, dict):
        return ""
    delta = document.get("delta")
    for candidate in (
        delta.get("stop_reason") if isinstance(delta, dict) else None,
        document.get("stop_reason"),
    ):
        found = _reason(candidate)
        if found:
            return found
    message = document.get("message")
    if isinstance(message, dict):
        found = _reason(message.get("stop_reason"))
        if found:
            return found
    for candidate in (document, document.get("response")):
        found = _incomplete(candidate)
        if found:
            return found
    choices = document.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return _reason(choices[0].get("finish_reason"))
    return ""


def _int(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def usage_from(document: object) -> Usage:
    if not isinstance(document, dict):
        return Usage()
    block = document.get("usage")
    if not isinstance(block, dict):
        message = document.get("message") or document.get("response")
        block = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(block, dict):
        return Usage()
    return Usage(
        input_tokens=_int(block.get("input_tokens", block.get("prompt_tokens"))),
        output_tokens=_int(block.get("output_tokens", block.get("completion_tokens"))),
        cache_creation_input_tokens=_int(block.get("cache_creation_input_tokens")),
        cache_read_input_tokens=_int(block.get("cache_read_input_tokens")),
    )


class UsageReader:
    MAX_PENDING = 1_000_000

    def __init__(self, content_type: str = "text/event-stream") -> None:
        self.usage = Usage()
        self.status = "unavailable"
        self.stop_reason = ""
        self._pending = ""
        self._data: list[str] = []
        self._data_chars = 0
        self._failed = False
        self._blocks: dict[tuple[str, object], list[str]] = {}
        self._characters = 0
        self._capped = False
        self._framed = 0
        self._unframed = False
        media_type = content_type.split(";", 1)[0].strip().lower()
        self._json = media_type == "application/json"
        self._supported = self._json or media_type == "text/event-stream"
        self._fields: set[str] = set()

    def feed(self, text: str) -> None:
        if self._failed or not self._supported:
            return
        self._pending += text
        if len(self._pending) + self._data_chars > self.MAX_PENDING:
            self._failed = True
            self._unframed = True
            self._pending = ""
            self._data.clear()
            self.status = "partial" if self._fields else "unavailable"
            return
        if self._json:
            return
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            line = line.removesuffix("\r")
            if not line:
                if self._data:
                    self._consume("\n".join(self._data))
                    self._data.clear()
                    self._data_chars = 0
            elif line.startswith("data:"):
                data = line[5:].removeprefix(" ")
                self._data.append(data)
                self._data_chars += len(data) + 1

    def finish(self) -> None:
        if self._json and not self._failed:
            self._consume(self._pending)
            self._pending = ""
        elif self._pending or self._data:
            self._unframed = True
            self.status = "partial" if self._fields else "unavailable"

    @property
    def response_status(self) -> str:
        if not self._supported:
            return "unavailable"
        if self._unframed:
            return "partial" if self._framed else "unavailable"
        return "partial" if self._capped else "known"

    def response_texts(self) -> list[tuple[str, str]]:
        return [
            (kind, "".join(parts)) for (kind, _index), parts in self._blocks.items()
        ]

    def forget(self) -> None:
        """The counts outlive the response; the response does not."""
        self._blocks.clear()

    def _absorb(self, document: dict) -> None:
        if document.get("type") == "content_block_delta":
            delta = document.get("delta")
            if isinstance(delta, dict):
                kind = _DELTA_KINDS.get(delta.get("type"))
                if kind is not None:
                    self._keep(kind, document.get("index"), delta.get(kind))
            return
        content = document.get("content")
        if not isinstance(content, list):
            message = document.get("message")
            content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for position, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") in RESPONSE_KINDS:
                kind = block["type"]
                self._keep(kind, position, block.get(kind))

    def _keep(self, kind: str, index: object, text: object) -> None:
        if not isinstance(text, str) or not text:
            return
        room = MAX_RESPONSE_CHARACTERS - self._characters
        if len(text) >= room:
            text = text[: max(0, room)]
            self._capped = True
            if not text:
                return
        self._characters += len(text)
        self._blocks.setdefault((kind, index), []).append(text)

    def _consume(self, text: str) -> None:
        if text == "[DONE]":
            return
        try:
            document = json.loads(text)
        except (ValueError, RecursionError):
            self._unframed = True
            self.status = "partial" if self._fields else "unavailable"
            return
        if not isinstance(document, dict):
            return
        self._framed += 1
        self._absorb(document)
        if not self.stop_reason:
            self.stop_reason = stop_reason_from(document)
        nested = document.get("message") or document.get("response") or document
        block = document.get("usage")
        if not isinstance(block, dict) and isinstance(nested, dict):
            block = nested.get("usage")
        if not isinstance(block, dict):
            return
        for label, aliases in (
            ("input", ("input_tokens", "prompt_tokens")),
            ("output", ("output_tokens", "completion_tokens")),
        ):
            if any(type(block.get(key)) is int and block[key] >= 0 for key in aliases):
                self._fields.add(label)
        self.usage = self.usage.merge(usage_from(document))
        self.status = (
            "known"
            if len(self._fields) == 2
            else "partial"
            if self._fields
            else "unavailable"
        )


def _bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False).encode()
    except (TypeError, ValueError):
        return b""


def _size(value: object) -> int:
    return len(_bytes(value))


def sections(document: object) -> dict:
    if not isinstance(document, dict):
        return {}
    found = {name: _size(document[name]) for name in SECTIONS if name in document}
    rest = sum(_size(value) for name, value in document.items() if name not in SECTIONS)
    if rest:
        found[OTHER] = rest
    return found


def _message_texts(document: dict):
    for message in document.get("messages") or ():
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            yield content
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    yield part["text"]


@dataclass(frozen=True)
class AtFiles:
    count: int = 0
    bytes: int = 0


def at_files(document: object) -> AtFiles:
    if not isinstance(document, dict):
        return AtFiles()
    count = 0
    total = 0
    for text in _message_texts(document):
        start = 0
        while True:
            start = text.find(_REMINDER_OPEN, start)
            if start < 0:
                break
            end = text.find(_REMINDER_CLOSE, start)
            if end < 0:
                break
            block = text[start : end + len(_REMINDER_CLOSE)]
            if AT_FILE_MARKER in block:
                count += 1
                total += len(block.encode())
            start = end + len(_REMINDER_CLOSE)
    return AtFiles(count, total)


def attribute(by_bytes: dict, exact_total: int) -> dict:
    total_bytes = sum(by_bytes.values())
    if not total_bytes or exact_total <= 0:
        return {}
    shares = {
        name: exact_total * size // total_bytes for name, size in by_bytes.items()
    }
    remainder = exact_total - sum(shares.values())
    if remainder:
        largest = max(by_bytes, key=lambda name: (by_bytes[name], name))
        shares[largest] += remainder
    return shares


def _section(path: tuple) -> str:
    name = path[0] if path else OTHER
    return name if name in SECTIONS else OTHER


def _opaque(document: dict, path: tuple) -> bool:
    """Base64 payloads and thinking signatures are large and are not prose."""
    key = path[-1] if path else None
    if key == "signature":
        return True
    if key != "data":
        return False
    parent: object = document
    for step in path[:-1]:
        try:
            parent = parent[step]  # type: ignore[index]
        except (KeyError, IndexError, TypeError):
            return False
    return isinstance(parent, dict) and parent.get("type") == "base64"


def _tally(leaves: list, evaluate) -> tuple[dict, dict]:
    counts: dict = {}
    named: dict = {}
    for _path, text in leaves:
        decision = evaluate(text)
        for entity, count in getattr(decision, "counts", ()):
            counts[entity] = counts.get(entity, 0) + count
        for name, count in getattr(decision, "custom_counts", ()):
            named[name] = named.get(name, 0) + count
    return counts, named


def scan_response(reader: UsageReader, evaluate) -> tuple[dict, dict]:
    counts: dict[str, dict] = {}
    named: dict = {}
    for kind, text in reader.response_texts():
        decision = evaluate(text)
        found = counts.setdefault(kind, {})
        for entity, count in getattr(decision, "counts", ()):
            found[entity] = found.get(entity, 0) + count
        for label, count in getattr(decision, "custom_counts", ()):
            named[label] = named.get(label, 0) + count
    return {kind: found for kind, found in counts.items() if found}, named


def _flatten(by_section: dict) -> dict:
    counts: dict = {}
    for section in by_section.values():
        for entity, count in section.items():
            counts[entity] = counts.get(entity, 0) + count
    return counts


class SectionMemo:
    """Counts for a repeated section, keyed by its hash. Never holds the text.

    `tools` and `system` arrive verbatim on every request of a session and are
    the largest sections; scanning them each time would hold a measurement slot
    for most of the request's own duration.
    """

    def __init__(self) -> None:
        self._counts: dict[str, tuple[dict, dict]] = {}
        self._lock = threading.Lock()

    def tally(self, section: str, value: object, leaves: list, evaluate) -> tuple:
        serialised = _bytes(value)
        if not serialised:
            return _tally(leaves, evaluate)
        key = f"{section}:{hashlib.sha256(serialised).hexdigest()}"
        with self._lock:
            hit = self._counts.get(key)
        if hit is not None:
            return hit
        counts = _tally(leaves, evaluate)
        with self._lock:
            self._counts[key] = counts
            while len(self._counts) > MEMO_LIMIT:
                del self._counts[next(iter(self._counts))]
        return counts


@dataclass
class Exchange:
    """Never retain traffic."""

    path: str = ""
    model: str = ""
    status: int = 0
    request_bytes: int = 0
    usage: Usage = field(default_factory=Usage)
    sections: dict = field(default_factory=dict)
    entities: dict = field(default_factory=dict)
    entities_by_section: dict = field(default_factory=dict)
    custom: dict = field(default_factory=dict)
    response_entities: dict = field(default_factory=dict)
    response_custom: dict = field(default_factory=dict)
    response_scan_status: str = "unavailable"
    stop_reason: str = ""
    at_files: AtFiles = field(default_factory=AtFiles)
    measured: bool = True
    # Why `measured` is False, so the report can say it. One of
    # INCOMPLETE_REASONS; empty when the request was measured.
    incomplete_reason: str = ""
    usage_status: str = "unavailable"

    def __post_init__(self) -> None:
        if self.entities_by_section and not self.entities:
            self.entities = _flatten(self.entities_by_section)

    def tokens_by_section(self) -> dict:
        return attribute(self.sections, self.usage.total_input)


def inspect_request(body: bytes | bytearray, evaluate=None, memo=None) -> Exchange:
    exchange = Exchange(request_bytes=len(body))
    if len(body) > MAX_BODY_BYTES:
        exchange.measured = False
        exchange.incomplete_reason = BODY_TOO_LARGE
        return exchange
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        exchange.measured = False
        exchange.incomplete_reason = NOT_JSON
        return exchange
    if isinstance(document, dict) and isinstance(document.get("model"), str):
        model = document["model"]
        exchange.model = (
            model
            if model and len(model) <= MAX_MODEL_CHARS and model.isprintable()
            else UNKNOWN_MODEL
        )
    exchange.sections = sections(document)
    exchange.at_files = at_files(document)
    if evaluate is not None and isinstance(document, dict):
        try:
            leaves = walk(
                document,
                max_leaves=MAX_SCAN_LEAVES,
                max_characters=MAX_BODY_BYTES,
                max_depth=MAX_SCAN_DEPTH,
            ).leaves
        except (PayloadTooLarge, RecursionError):
            # Half a count reads as a whole one; say the request was not measured.
            exchange.measured = False
            exchange.incomplete_reason = TOO_MANY_FIELDS
            return exchange
        grouped: dict[str, list] = {}
        for leaf in leaves:
            if not _opaque(document, leaf[0]):
                grouped.setdefault(_section(leaf[0]), []).append(leaf)
        found: dict[str, dict] = {}
        named: dict = {}
        for name, group in grouped.items():
            counts, custom = (
                memo.tally(name, document.get(name), group, evaluate)
                if memo is not None and name in MEMOISED
                else _tally(group, evaluate)
            )
            if counts:
                found[name] = counts
            for label, count in custom.items():
                named[label] = named.get(label, 0) + count
        exchange.entities_by_section = found
        exchange.entities = _flatten(found)
        exchange.custom = named
    return exchange


__all__ = [
    "AT_FILE_MARKER",
    "MAX_BODY_BYTES",
    "MAX_MODEL_CHARS",
    "MAX_RESPONSE_CHARACTERS",
    "MAX_STOP_REASON_CHARS",
    "MAX_SCAN_DEPTH",
    "MAX_SCAN_LEAVES",
    "MEMOISED",
    "MEMO_LIMIT",
    "OTHER",
    "RESPONSE_KINDS",
    "SECTIONS",
    "TRUNCATED",
    "UNKNOWN_MODEL",
    "UNKNOWN_REASON",
    "AtFiles",
    "Exchange",
    "SectionMemo",
    "Usage",
    "UsageReader",
    "at_files",
    "attribute",
    "inspect_request",
    "scan_response",
    "sections",
    "stop_reason_from",
    "usage_from",
]
