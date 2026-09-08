from __future__ import annotations

from dataclasses import dataclass

NOT_INSPECTED = "not inspected"
MAX_DISPLAY_LABEL_CHARS = 120
UNKNOWN_TOOL_LABEL = "unknown tool"
UNSUPPORTED_EVENT_LABEL = "unsupported tool event"


def display_label(text: str, fallback: str) -> str:
    if (
        not text.isprintable()
        or not text.strip()
        or len(text) > MAX_DISPLAY_LABEL_CHARS
    ):
        return fallback
    return text


@dataclass(frozen=True)
class Record:
    client: str
    event: str
    tool_name: str
    direction: str
    mode: str
    action: str
    entities: tuple = ()
    target: str = ""
    in_bytes: int = 0
    out_bytes: int = 0
    fields: int = 0
    transforms: tuple = ()
    markers: tuple = ()
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "client": self.client,
            "event": self.event,
            "tool_name": self.tool_name,
            "target": self.target,
            "direction": self.direction,
            "mode": self.mode,
            "action": self.action,
            "entities": dict(self.entities),
            "in_bytes": self.in_bytes,
            "out_bytes": self.out_bytes,
            "fields": self.fields,
            "transforms": list(self.transforms),
            "markers": list(self.markers),
            "note": self.note,
        }


def _timestamp() -> str:
    import datetime

    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def remember(
    session_id: str, record: Record, latency_ms: int, ledger: bool = False
) -> None:
    if not session_id:
        return
    try:
        from . import spool

        entry = record.as_dict()
        entry["session_id"] = spool.session_key(session_id)
        entry["latency_ms"] = latency_ms
        entry["ts"] = _timestamp()
    except Exception:
        return
    try:
        spool.append(session_id, entry)
    except Exception:
        pass
    if not ledger:
        return
    try:
        from . import ledger as store

        store.append(entry)
    except Exception:
        pass
