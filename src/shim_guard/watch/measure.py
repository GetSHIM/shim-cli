from __future__ import annotations

import json
from dataclasses import dataclass, field

SECTIONS = ("tools", "system", "messages")
OTHER = "other"

MAX_BODY_BYTES = 8_000_000

MAX_MODEL_CHARS = 120
UNKNOWN_MODEL = "unknown"

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
        self._pending = ""
        self._data: list[str] = []
        self._data_chars = 0
        self._failed = False
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
            self.status = "partial" if self._fields else "unavailable"

    def _consume(self, text: str) -> None:
        if text == "[DONE]":
            return
        try:
            document = json.loads(text)
        except (ValueError, RecursionError):
            self.status = "partial" if self._fields else "unavailable"
            return
        if not isinstance(document, dict):
            return
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


def _size(value: object) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False).encode())
    except (TypeError, ValueError):
        return 0


def sections(document: object) -> dict:
    if not isinstance(document, dict):
        return {}
    found = {name: _size(document[name]) for name in SECTIONS if name in document}
    rest = sum(_size(value) for name, value in document.items() if name not in SECTIONS)
    if rest:
        found[OTHER] = rest
    return found


def _texts(document: dict):
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
    for text in _texts(document):
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
    at_files: AtFiles = field(default_factory=AtFiles)
    measured: bool = True
    usage_status: str = "unavailable"

    def tokens_by_section(self) -> dict:
        return attribute(self.sections, self.usage.total_input)


def inspect_request(body: bytes | bytearray, evaluate=None) -> Exchange:
    exchange = Exchange(request_bytes=len(body))
    if len(body) > MAX_BODY_BYTES:
        exchange.measured = False
        return exchange
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        exchange.measured = False
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
        counts: dict = {}
        for text in _texts(document):
            decision = evaluate(text)
            for entity, count in getattr(decision, "counts", ()):
                counts[entity] = counts.get(entity, 0) + count
        exchange.entities = counts
    return exchange


__all__ = [
    "AT_FILE_MARKER",
    "MAX_BODY_BYTES",
    "MAX_MODEL_CHARS",
    "OTHER",
    "SECTIONS",
    "UNKNOWN_MODEL",
    "AtFiles",
    "Exchange",
    "Usage",
    "UsageReader",
    "at_files",
    "attribute",
    "inspect_request",
    "sections",
    "usage_from",
]
