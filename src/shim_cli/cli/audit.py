from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import perf_counter

import typer

from shim_cli.cli.output import emit_error, emit_json
from shim_cli.guard.entities import PLACEHOLDER
from shim_cli.session.record import UNKNOWN_TOOL_LABEL, display_label, scrubbed_target

MAX_LINE = 8_000_000
PROMPT = "your prompt"
_PROGRESS_AFTER_SECONDS = 2.0
_SHOWN_DOORS = 3


@dataclass
class Session:
    id: str
    project: str
    last: float = 0.0
    reached: dict[str, dict[str, int]] = field(default_factory=dict)
    output: dict[str, int] = field(default_factory=dict)
    masked: int = 0
    files: dict[Path, tuple[int, int]] = field(default_factory=dict)


@dataclass
class _Scan:
    sessions: list[Session] = field(default_factory=list)
    first: float = 0.0
    last: float = 0.0
    size: int = 0
    skipped: int = 0
    uninspected: int = 0


def _projects_folder() -> Path:
    from shim_cli.clients.claude.settings import target_path

    return target_path().parent / "projects"


def _home_form(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def _plural(count: int, word: str, plural: str = "") -> str:
    return f"{count:,} {word if count == 1 else plural or word + 's'}"


def _ordered(counts: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _moment(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        datetime.fromtimestamp(moment)
    except (ValueError, OverflowError, OSError):
        return None
    return moment


def _text_field(value: object) -> str:
    return value if isinstance(value, str) else ""


def _size(count: int) -> str:
    if count < 1_000_000:
        return f"{-(-count // 1000):,} KB"
    return f"{count / 1_000_000:,.1f} MB"


def _date(moment: float) -> str:
    return datetime.fromtimestamp(moment).strftime("%Y-%m-%d")


def _minute(moment: float) -> str:
    return datetime.fromtimestamp(moment).strftime("%Y-%m-%d %H:%M")


def _under(cwd: str, project: str | None) -> bool:
    return (
        project is None
        or cwd == project
        or cwd.startswith(project.rstrip(os.sep) + os.sep)
    )


def _session_files(projects: Path) -> list[tuple[str, list[Path]]]:
    found = []
    for folder in sorted(os.scandir(projects), key=lambda entry: entry.name):
        if not folder.is_dir(follow_symlinks=False):
            continue
        for entry in sorted(os.scandir(folder.path), key=lambda entry: entry.name):
            if not entry.name.endswith(".jsonl") or not entry.is_file(
                follow_symlinks=False
            ):
                continue
            session = entry.name[: -len(".jsonl")]
            if session in ("", ".", ".."):
                continue
            files = [Path(entry.path)]
            agents = Path(folder.path, session, "subagents")
            if (
                agents.is_dir()
                and not agents.parent.is_symlink()
                and not agents.is_symlink()
            ):
                files += sorted(
                    Path(agent.path)
                    for agent in os.scandir(agents)
                    if agent.name.endswith(".jsonl")
                    and agent.is_file(follow_symlinks=False)
                )
            found.append((session, files))
    return found


def _lines(path: Path) -> Iterator[bytes | None]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        while line := stream.readline(MAX_LINE + 1):
            if len(line) > MAX_LINE and not line.endswith(b"\n"):
                while line and not line.endswith(b"\n"):
                    line = stream.readline(MAX_LINE + 1)
                yield None
                continue
            yield line


def _records(path: Path, scan: _Scan) -> Iterator[dict]:
    for line in _lines(path):
        try:
            record = json.loads(line) if line is not None else None
        except (ValueError, RecursionError):
            record = None
        if isinstance(record, dict):
            yield record
        else:
            scan.skipped += 1


def _first_cwd(path: Path) -> str | None:
    for record in _records(path, _Scan()):
        if isinstance(record.get("cwd"), str):
            return record["cwd"]
    return None


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )


def _texts(
    record: dict, tools: dict[str, str], subagent: bool
) -> Iterator[tuple[str, str, str, str]]:
    kind = record.get("type")
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if kind == "assistant" and isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                tools[block["id"]] = _text_field(block.get("name"))
            elif block.get("type") in ("text", "thinking"):
                written = block.get(block["type"])
                yield "output", "", "", written if isinstance(written, str) else ""
    elif kind == "user" and not record.get("isCompactSummary"):
        if isinstance(content, str) and not subagent:
            yield "reached", PROMPT, "", content
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and not subagent:
                    yield "reached", PROMPT, "", _text([block])
                elif block.get("type") == "tool_result":
                    tool = display_label(
                        tools.get(str(block.get("tool_use_id")), ""),
                        UNKNOWN_TOOL_LABEL,
                    )
                    failed = block.get("is_error") is True
                    door = "failed " if failed else ""
                    yield "tool", door, tool, _text(block.get("content"))
    elif kind == "attachment" and isinstance(record.get("attachment"), dict):
        attachment = record["attachment"]
        flavour = attachment.get("type")
        if flavour == "file":
            body = attachment.get("content")
            if isinstance(body, dict):
                file = body.get("file")
                body = file.get("content") if isinstance(file, dict) else None
            if isinstance(body, str):
                path = _text_field(attachment.get("displayPath"))
                yield "reached", "@", path, body
        elif flavour == "edited_text_file":
            name = os.path.basename(_text_field(attachment.get("filename")))
            yield "reached", "edited ", name, _text(attachment.get("snippet"))
        elif flavour == "queued_command" and not subagent:
            yield "reached", PROMPT, "", _text(attachment.get("prompt"))


def _scan(
    projects: Path,
    since: float | None,
    project: str | None,
    evaluate: Callable,
    progress: Callable[[int, int], None],
) -> _Scan:
    scan = _Scan()
    names: dict[str, str] = {}

    def scrub(value: str) -> str:
        if value not in names:
            try:
                names[value] = scrubbed_target(value, evaluate) or "…"
            except Exception:
                names[value] = "…"
        return names[value]

    listed = _session_files(projects)
    total = sum(len(files) for _identifier, files in listed)
    done = 0
    for identifier, files in listed:
        done += len(files)
        progress(done, total)
        try:
            cwd = _first_cwd(files[0])
        except OSError:
            scan.skipped += 1
            continue
        if cwd is None or not _under(cwd, project):
            continue
        session = Session(identifier, scrub(_home_form(cwd)))
        for index, path in enumerate(files):
            tools: dict[str, str] = {}
            try:
                status = os.lstat(path)
                session.files[path] = (status.st_size, status.st_mtime_ns)
                if since is not None and status.st_mtime < since:
                    continue
                scan.size += status.st_size
                for record in _records(path, scan):
                    moment = _moment(record.get("timestamp"))
                    if moment is not None and since is not None and moment < since:
                        continue
                    if moment is not None:
                        session.last = max(session.last, moment)
                        scan.first = min(scan.first or moment, moment)
                        scan.last = max(scan.last, moment)
                    for kind, door, name, text in _texts(record, tools, index > 0):
                        if not text:
                            continue
                        if kind == "tool":
                            session.masked += len(PLACEHOLDER.findall(text))
                        try:
                            decision = evaluate(text)
                        except Exception:
                            scan.uninspected += 1
                            continue
                        scan.uninspected += decision.partial
                        label = door + scrub(name) if name else door
                        for entity, count in decision.counts:
                            if kind == "output":
                                session.output[entity] = (
                                    session.output.get(entity, 0) + count
                                )
                            else:
                                doors = session.reached.setdefault(entity, {})
                                doors[label] = doors.get(label, 0) + count
            except OSError:
                scan.skipped += 1
        if session.last:
            scan.sessions.append(session)
    return scan


def _summary(scan: _Scan, seconds: float) -> dict:
    reached: dict[str, dict] = {}
    by_project: dict[str, dict] = {}
    by_session: dict[str, dict] = {}
    output: dict[str, int] = {}
    for session in scan.sessions:
        counts = {
            entity: sum(doors.values()) for entity, doors in session.reached.items()
        }
        for entity, doors in session.reached.items():
            entry = reached.setdefault(entity, {"total": 0, "sessions": 0, "doors": {}})
            entry["total"] += counts[entity]
            entry["sessions"] += 1
            for door, count in doors.items():
                entry["doors"][door] = entry["doors"].get(door, 0) + count
        if counts:
            place = by_project.setdefault(session.project, {"counts": {}, "last": ""})
            for entity, count in counts.items():
                place["counts"][entity] = place["counts"].get(entity, 0) + count
            place["last"] = max(place["last"], _date(session.last))
        for entity, count in session.output.items():
            output[entity] = output.get(entity, 0) + count
        by_session[session.id] = {
            "project": session.project,
            "last": _date(session.last),
            "reached": counts,
            "model_output": dict(session.output),
            "masked": session.masked,
        }
    return {
        "sessions": len(scan.sessions),
        "projects": len({session.project for session in scan.sessions}),
        "first": _date(scan.first) if scan.first else None,
        "last": _date(scan.last) if scan.last else None,
        "reached": reached,
        "by_project": by_project,
        "by_session": by_session,
        "masked": {
            "values": sum(session.masked for session in scan.sessions),
            "sessions": sum(session.masked > 0 for session in scan.sessions),
        },
        "model_output": output,
        "scanned": {
            "sessions": len(scan.sessions),
            "bytes": scan.size,
            "seconds": round(seconds, 1),
            "skipped_lines": scan.skipped,
            "uninspected_texts": scan.uninspected,
        },
    }


def _report_lines(summary: dict) -> list[str]:
    from shim_cli.session.summary import NOT_LEAKS

    lines = [
        "shim audit — Claude Code history, "
        f"{_plural(summary['sessions'], 'session')} in "
        f"{_plural(summary['projects'], 'project')}, "
        f"{summary['first']} to {summary['last']}",
        "",
        "reached the model",
    ]
    if not summary["reached"]:
        lines.append("  nothing")
    for entity, entry in sorted(
        summary["reached"].items(), key=lambda item: (-item[1]["total"], item[0])
    ):
        doors = _ordered(entry["doors"])
        shown = " · ".join(f"{door} {count:,}" for door, count in doors[:_SHOWN_DOORS])
        if len(doors) > _SHOWN_DOORS:
            shown += f" · +{len(doors) - _SHOWN_DOORS} more"
        lines.append(
            f"  {entity:<14}{entry['total']:>4}  in "
            f"{_plural(entry['sessions'], 'session')}   {shown}"
        )
    if summary["by_project"]:
        lines += ["", "by project"]
        for name, place in sorted(
            summary["by_project"].items(),
            key=lambda item: (-sum(item[1]["counts"].values()), item[0]),
        ):
            counts = ", ".join(
                f"{entity} {count:,}" for entity, count in _ordered(place["counts"])
            )
            lines.append(f"  {name:<26} {counts} · last {place['last']}")
    masked = summary["masked"]
    values = (
        f"{_plural(masked['values'], 'value')} in "
        f"{_plural(masked['sessions'], 'session')}"
        if masked["values"]
        else "none"
    )
    written = ", ".join(
        f"{count:,} {entity}" for entity, count in _ordered(summary["model_output"])
    )
    lines += [
        "",
        f"{'already masked by shim':<29}{values}",
        f"{'model output':<29}{f'{written} ({NOT_LEAKS})' if written else 'none'}",
        "",
    ]
    scanned = summary["scanned"]
    skipped = []
    if scanned["skipped_lines"]:
        skipped.append(
            f"{_plural(scanned['skipped_lines'], 'line')} that could not be read"
        )
    if scanned["uninspected_texts"]:
        skipped.append(
            f"{_plural(scanned['uninspected_texts'], 'text')} the detector could not "
            "analyse"
        )
    if skipped:
        lines.append(f"skipped {' and '.join(skipped)}.")
    lines.append(
        f"scanned {_plural(scanned['sessions'], 'session')}, "
        f"{_size(scanned['bytes'])}, in {scanned['seconds']:.1f} s."
    )
    return lines


def _run_scan(
    *, since: str | None, project: Path | None, as_json: bool
) -> tuple[_Scan, dict]:
    from shim_cli.cli.configuration import (
        FIX_SETTINGS_INVALID,
        FIX_SETTINGS_UNSAFE,
        settings_refused,
    )
    from shim_cli.config import describe_settings_error, load_policy
    from shim_cli.guard import evaluate as evaluate_guard

    projects = _projects_folder()
    shown = _home_form(str(projects))
    start = None
    if since is not None:
        try:
            start = datetime.strptime(since, "%Y-%m-%d").timestamp()
        except ValueError:
            emit_error(
                "audit",
                "INVALID_DATE",
                "--since takes a date as YYYY-MM-DD",
                "Pass --since as YYYY-MM-DD.",
                as_json=as_json,
                plain=True,
            )
    if not projects.is_dir():
        emit_error(
            "audit",
            "HISTORY_NOT_FOUND",
            f"no Claude Code history at {shown}",
            None,
            as_json=as_json,
            plain=True,
        )
    try:
        policy = load_policy()
    except ValueError as error:
        problem = describe_settings_error(error).rstrip(".")
        if settings_refused(error):
            emit_error(
                "audit",
                "SETTINGS_REFUSED",
                problem,
                FIX_SETTINGS_UNSAFE,
                as_json=as_json,
                plain=True,
            )
        emit_error(
            "audit",
            "SETTINGS_INVALID",
            problem,
            FIX_SETTINGS_INVALID,
            as_json=as_json,
            plain=True,
        )
    started = perf_counter()
    widths: list[int] = []

    def progress(done: int, total: int) -> None:
        if sys.stderr.isatty() and perf_counter() - started > _PROGRESS_AFTER_SECONDS:
            line = f"\rshim audit: {done:,} of {total:,} files"
            sys.stderr.write(line)
            sys.stderr.flush()
            widths.append(len(line))

    try:
        scan = _scan(
            projects,
            start,
            str(project.expanduser().resolve()) if project is not None else None,
            lambda text: evaluate_guard(text, policy.entities, policy.custom),
            progress,
        )
    except OSError:
        emit_error(
            "audit",
            "HISTORY_UNREADABLE",
            f"the Claude Code history at {shown} could not be read",
            None,
            as_json=as_json,
            plain=True,
        )
    if widths:
        sys.stderr.write("\r" + " " * max(widths) + "\r")
    return scan, _summary(scan, perf_counter() - started)


def _terminal() -> bool:
    return sys.stdin.isatty()


def _status(path: Path) -> tuple[int, int] | None:
    try:
        status = os.lstat(path)
    except OSError:
        return None
    return status.st_size, status.st_mtime_ns


def _history_session(line: bytes) -> str:
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        return ""
    return _text_field(record.get("sessionId")) if isinstance(record, dict) else ""


def _forget(history: Path, sessions: set[str]) -> int | None:
    if not sessions or history.is_symlink() or not history.is_file():
        return 0
    before = _status(history)
    lines = history.read_bytes().splitlines(keepends=True)
    kept = [line for line in lines if _history_session(line) not in sessions]
    if len(kept) == len(lines):
        return 0
    descriptor, temporary = tempfile.mkstemp(
        dir=history.parent, prefix=".history-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(history.stat().st_mode))
            stream.writelines(kept)
        if _status(history) != before:
            os.unlink(temporary)
            return None
        os.replace(temporary, history)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return len(lines) - len(kept)


def _where(session: Session) -> str:
    return f"{session.project} {_minute(session.last)}"


def _purge(scan: _Scan, summary: dict) -> None:
    typer.echo("\n".join(_report_lines(summary)))
    eligible = [session for session in scan.sessions if session.reached]
    if not eligible:
        typer.echo("shim: no session sent anything to the model; nothing was deleted.")
        return
    typer.echo("\nsessions that sent something to the model")
    for session in eligible:
        totals = {
            entity: sum(doors.values()) for entity, doors in session.reached.items()
        }
        counts = ", ".join(f"{entity} {count:,}" for entity, count in _ordered(totals))
        typer.echo(f"  {session.project:<26} {_minute(session.last)}  {counts}")
    total = len(eligible)
    these = "this session" if total == 1 else f"these {total} sessions"
    typer.echo(f"\nDelete {these} from this computer? This cannot be undone.")
    typer.echo(
        "Close Claude Code first. What was already sent to the model provider is "
        "not affected."
    )
    try:
        answer = input(f'Type "delete {total}" to continue: ')
    except EOFError:
        answer = ""
    if answer != f"delete {total}":
        typer.echo("shim: nothing was deleted.")
        return
    ready = []
    for session in eligible:
        if any(_status(path) != status for path, status in session.files.items()):
            typer.echo(f"{_where(session)}: changed during the run, not deleted")
        else:
            ready.append(session)
    try:
        removed = _forget(
            _projects_folder().parent / "history.jsonl",
            {session.id for session in ready},
        )
    except OSError as error:
        typer.echo(
            f"shim: history.jsonl could not be rewritten ({error.strerror}); "
            "nothing was deleted."
        )
        return
    if removed is None:
        typer.echo(
            "shim: history.jsonl changed while shim was writing; nothing was "
            "deleted. Close Claude Code and run the command again."
        )
        return
    deleted = folders = 0
    for session in ready:
        transcript = next(iter(session.files))
        folder = transcript.parent / transcript.name[: -len(".jsonl")]
        try:
            if folder.is_symlink():
                os.unlink(folder)
                folders += 1
            elif folder.is_dir():
                shutil.rmtree(folder)
                folders += 1
        except OSError:
            typer.echo(
                f"{_where(session)}: its folder could not be fully deleted, so its "
                "transcript was kept"
            )
            continue
        try:
            os.unlink(transcript)
        except OSError:
            typer.echo(f"{_where(session)}: could not be deleted")
            continue
        deleted += 1
    typer.echo(
        f"deleted {_plural(deleted, 'session')} ({_plural(deleted, 'file')}, "
        f"{_plural(folders, 'directory', 'directories')}) and "
        f"{_plural(removed, 'line')} of history.jsonl, on this computer only."
    )


def audit(
    *, since: str | None, project: Path | None, as_json: bool, purge: bool = False
) -> None:
    if purge and as_json:
        emit_error(
            "audit",
            "OPTIONS_CONFLICT",
            "--purge cannot be combined with --json; nothing was deleted",
            "Run the command with one of them.",
            as_json=True,
        )
    if purge and not _terminal():
        emit_error(
            "audit",
            "TERMINAL_REQUIRED",
            "--purge needs a terminal; nothing was deleted",
            "Run shim audit --purge in a terminal.",
            as_json=False,
            plain=True,
        )
    scan, summary = _run_scan(since=since, project=project, as_json=as_json)
    found = bool(summary["reached"])
    if as_json:
        emit_json("audit", "findings" if found else "safe", **summary)
    elif not scan.sessions:
        typer.echo("shim: there are no Claude Code sessions to audit.")
    elif purge:
        _purge(scan, summary)
    else:
        lines = _report_lines(summary)
        lines[-1] += " Nothing was changed, written or sent."
        typer.echo("\n".join(lines))
    if found:
        raise typer.Exit(1)
