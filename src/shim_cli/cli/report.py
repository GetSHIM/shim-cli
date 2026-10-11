from __future__ import annotations

import sys

import typer

from shim_cli.cli import migration
from shim_cli.cli.output import (
    FIX_CONFIRMATION,
    emit,
    emit_error,
    emit_json,
    terminal_text,
)
from shim_cli.session import spool, summary

_FIX_LEDGER = "Run shim doctor claude (or codex, copilot)."


def _retained() -> list:
    from shim_cli.session import ledger

    try:
        entries = ledger.entries()
    except (ledger.LedgerError, OSError):
        return []
    if not entries:
        return []
    newest = max(entries, key=lambda entry: str(entry.get("ts", "")))
    session = newest.get("session_id")
    return [entry for entry in entries if entry.get("session_id") == session]


def report(*, as_json: bool) -> None:
    migration.announce(migration.ledger_files(), as_json=as_json)
    try:
        stem = spool.newest()
        records = spool.entries_for_stem(stem) if stem else []
        truncated = spool.capped_for_stem(stem) if stem else False
    except (spool.SpoolError, OSError):
        emit_error(
            "report",
            "RECORDS_UNREADABLE",
            "Session records could not be read.",
            "Run shim doctor claude (or codex, copilot); its session_record line "
            "names the cause.",
            as_json=as_json,
        )

    source = "session"
    if not records:
        records = _retained()
        source = "ledger" if records else "none"

    if as_json:
        document = summary.as_json(records, truncated)
        emit_json("report", "ok", source=source, **document)
        return

    if not records:
        emit(
            "WARN",
            "No session on record. shim writes one while a client session is open "
            "and deletes it when the session ends; `shim config --ledger` keeps it.",
        )
        raise typer.Exit(1)

    text = summary.render(records, truncated)
    if not text:
        count = len(records)
        plural = "" if count == 1 else "s"
        emit("PASS", f"shim inspected {count} event{plural} and found nothing.")
        return
    print(terminal_text(text, sys.stdout, "\n"))
    if source == "ledger":
        emit("WARN", "From the retained ledger; the live session has ended.")


def purge(*, yes: bool, as_json: bool) -> None:
    from shim_cli.session import ledger

    migration.announce(migration.ledger_files(), as_json=as_json)

    try:
        existing = ledger.files()
    except (ledger.LedgerError, OSError):
        emit_error(
            "ledger-purge",
            "LEDGER_UNREADABLE",
            "The ledger could not be read.",
            _FIX_LEDGER,
            as_json=as_json,
        )

    if not existing:
        if as_json:
            emit_json("ledger-purge", "ok", removed=0)
        else:
            emit("PASS", "There is nothing retained to delete.")
        return

    if not as_json and not yes:
        emit("WARN", f"{len(existing)} retained file(s) at {ledger.root_path()}.")
        if not typer.confirm("Delete them?", default=False):
            emit("WARN", "Nothing was deleted.")
            raise typer.Exit(1)
    if as_json and not yes:
        emit_error(
            "ledger-purge",
            "CONFIRMATION_REQUIRED",
            "--yes is required with --json",
            FIX_CONFIRMATION,
            as_json=True,
        )

    removed = ledger.purge()
    if as_json:
        emit_json("ledger-purge", "ok", removed=removed)
    else:
        emit("PASS", f"Deleted {removed} retained file(s).")


def show_ledger(*, as_json: bool) -> None:
    """Read back what the opt-in ledger kept.

    Turning the ledger on and then only being able to `purge` it made the
    record write-only: the person who enabled it to prove something to a
    colleague had to read JSONL by hand.
    """
    from shim_cli.session import ledger

    migration.announce(migration.ledger_files(), as_json=as_json)

    try:
        entries = ledger.entries()
    except (ledger.LedgerError, OSError):
        emit_error(
            "ledger-show",
            "LEDGER_UNREADABLE",
            "The ledger could not be read.",
            _FIX_LEDGER,
            as_json=as_json,
        )

    if not entries:
        if as_json:
            emit_json("ledger-show", "ok", days=0, events=0, entries=[])
        else:
            from shim_cli.config import load_policy

            try:
                on = load_policy().ledger
            except (OSError, ValueError):
                on = False
            emit(
                "PASS",
                "The ledger is on and has recorded nothing yet."
                if on
                else "The ledger is empty. Turn it on with `shim config --ledger`.",
            )
        return

    if as_json:
        emit_json(
            "ledger-show",
            "ok",
            days=len({str(entry.get("ts", ""))[:10] for entry in entries}),
            events=len(entries),
            entries=entries,
        )
        return

    by_day: dict[str, list] = {}
    for entry in entries:
        by_day.setdefault(str(entry.get("ts", ""))[:10] or "unknown", []).append(entry)

    plural = "" if len(entries) == 1 else "s"
    lines = [
        f"shim ledger — {len(entries)} event{plural} over {len(by_day)} day(s), "
        f"kept for {ledger.RETENTION_DAYS} days"
    ]
    for day, records in sorted(by_day.items()):
        counts: dict[str, int] = {}
        for record in records:
            found = record.get("entities")
            if isinstance(found, dict):
                for name, count in found.items():
                    if isinstance(name, str) and isinstance(count, int):
                        counts[name] = counts.get(name, 0) + count
        listed = ", ".join(
            f"{count} {name}"
            for name, count in sorted(counts.items(), key=lambda p: (-p[1], p[0]))
        )
        day_plural = "" if len(records) == 1 else "s"
        detail = f"   {listed}" if listed else "   nothing found"
        lines.append(f"  {day}   {len(records)} event{day_plural}{detail}")
    print(terminal_text("\n".join(lines), sys.stdout, "\n"))
