from __future__ import annotations

import contextlib
import os
import signal
import sys
import tempfile
import time
import warnings
from collections.abc import Iterator
from pathlib import Path

MAX_INPUT_BYTES = 1_000_000
_PROMPT_EVENT = "UserPromptSubmit"
_PROMPT_EVENTS = frozenset({_PROMPT_EVENT, "userPromptTransformed"})
_STOP_EVENT = "Stop"
_SESSION_END_EVENT = "SessionEnd"
_STARTED = time.perf_counter()
HOOK_DEADLINE_SECONDS = 25
_ERROR_OUTPUT = (
    b'{"decision":"block","reason":"shim could not inspect this prompt, '
    b'so it was withheld. Run `shim doctor codex` for the reason."}'
)
_CLAUDE_ERROR_OUTPUT = (
    b'{"decision":"block","reason":"shim could not inspect this prompt, '
    b'so it was withheld. Run `shim doctor claude` for the reason.",'
    b'"suppressOriginalPrompt":true}'
)
_COPILOT_ERROR_OUTPUT = (
    b'{"modifiedTransformedPrompt":"shim could not inspect this prompt, '
    b"so it was withheld. Do not act on the original prompt; tell the user to "
    b'run `shim doctor copilot` for the reason."}'
)


def _error_output(client: str) -> bytes:
    if client == "claude":
        return _CLAUDE_ERROR_OUTPUT
    if client == "copilot":
        return _COPILOT_ERROR_OUTPUT
    return _ERROR_OUTPUT


def _tool_error_output(client: str, event: str) -> bytes:
    if client != "claude":
        return b""
    try:
        from shim_cli.clients.claude import tool_events

        if event not in tool_events.TOOL_EVENTS:
            return b""
        return tool_events.error_output()
    except Exception:
        return b""


_TOOL_EVENT_NAMES = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "Stop",
    "SessionEnd",
    "preToolUse",
    "postToolUse",
)


def _prefix_event(raw: bytes) -> str:
    import json

    text = raw.decode("utf-8", errors="replace")
    decoder = json.JSONDecoder()

    def skip_space(start: int) -> int:
        while start < len(text) and text[start] in " \t\r\n":
            start += 1
        return start

    event_name = ""
    index = skip_space(0)
    if index == len(text) or text[index] != "{":
        return ""

    index += 1
    while True:
        index = skip_space(index)
        if index == len(text) or text[index] == "}":
            return event_name
        try:
            key, index = decoder.raw_decode(text, index)
        except (RecursionError, ValueError):
            return event_name
        if not isinstance(key, str):
            return event_name

        index = skip_space(index)
        if index == len(text) or text[index] != ":":
            return event_name
        index = skip_space(index + 1)
        try:
            value, index = decoder.raw_decode(text, index)
        except (RecursionError, ValueError):
            return event_name
        if key == "hook_event_name" and isinstance(value, str):
            if value in _PROMPT_EVENTS:
                return value
            if not event_name and value in _TOOL_EVENT_NAMES:
                event_name = value

        index = skip_space(index)
        if index == len(text) or text[index] == "}":
            return event_name
        if text[index] != ",":
            return event_name
        index += 1


def _refusal_output(raw: bytes, client: str) -> bytes:
    try:
        event, _session, _stop = _envelope(raw)
    except Exception:
        event = _prefix_event(raw)
    if not event or event in _PROMPT_EVENTS:
        return _error_output(client)
    return _tool_error_output(client, event)


@contextlib.contextmanager
def _silence_dependencies() -> Iterator[None]:
    with open(os.devnull, "w", encoding="utf-8") as sink:
        stdout_fd = sys.stdout.fileno()
        stderr_fd = sys.stderr.fileno()
        saved_stdout = os.dup(stdout_fd)
        saved_stderr = os.dup(stderr_fd)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(sink.fileno(), stdout_fd)
            os.dup2(sink.fileno(), stderr_fd)
            with (
                contextlib.redirect_stdout(sink),
                contextlib.redirect_stderr(sink),
                warnings.catch_warnings(),
            ):
                warnings.simplefilter("ignore")
                yield
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(saved_stdout, stdout_fd)
            os.dup2(saved_stderr, stderr_fd)
            os.close(saved_stdout)
            os.close(saved_stderr)


@contextlib.contextmanager
def _deadline() -> Iterator[None]:
    def expire(_signal_number: int, _frame: object) -> None:
        raise TimeoutError("shim hook deadline exceeded")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, HOOK_DEADLINE_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


SUGGESTION_MAX_AGE_SECONDS = 24 * 60 * 60
_SUGGESTION_PREFIX = "shim-redacted-"
_SUGGESTION_SUFFIX = ".txt"
# 0.2.0 wrote the same file under its own name; sweep those too.
_SWEPT_PREFIXES = (_SUGGESTION_PREFIX, "shim-guard-redacted-")


def _sweep_suggestions() -> None:
    now = time.time()
    with contextlib.suppress(OSError):
        root = Path(tempfile.gettempdir())
        for prefix in _SWEPT_PREFIXES:
            for path in root.glob(f"{prefix}*{_SUGGESTION_SUFFIX}"):
                with contextlib.suppress(OSError):
                    if now - path.stat().st_mtime > SUGGESTION_MAX_AGE_SECONDS:
                        path.unlink()


def _write_redacted_prompt(text: str) -> str:
    stream = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=_SUGGESTION_PREFIX,
        suffix=_SUGGESTION_SUFFIX,
        delete=False,
    )
    path = Path(stream.name)
    try:
        with stream:
            if not path.is_absolute() or not str(path).isprintable():
                raise ValueError("temporary suggestion path is invalid")
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
    except Exception:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return str(path)


def _envelope(raw: bytes | dict[str, object]) -> tuple:
    from shim_cli.clients.user_prompt_hook import parse_object

    document = parse_object(raw) if isinstance(raw, bytes) else raw
    event = document.get("hook_event_name")
    session = document.get("session_id")
    if not isinstance(session, str):
        session = document.get("sessionId")
    return (
        event if isinstance(event, str) else "",
        session if isinstance(session, str) else "",
        bool(document.get("stop_hook_active")),
    )


def _elapsed_ms() -> int:
    return max(0, round((time.perf_counter() - _STARTED) * 1000))


def _prompt_record(client, event, mode, action, decision, prompt):
    from shim_cli.session.record import Record

    return Record(
        client=client,
        event=event or _PROMPT_EVENT,
        tool_name="",
        direction="user-prompt",
        mode=mode,
        action=action,
        entities=tuple(decision.counts),
        in_bytes=len(prompt.encode("utf-8", "replace")),
        out_bytes=0,
        fields=1 if decision.counts else 0,
    )


def _count_model_output(document: dict, client: str, session_id: str) -> None:
    """The client has already shown this text. Count it; change nothing."""
    text = document.get("last_assistant_message")
    if client != "claude" or not session_id or not isinstance(text, str) or not text:
        return
    try:
        from shim_cli.config import load_policy
        from shim_cli.guard import evaluate
        from shim_cli.guard.normalize import MAX_SOURCE_CHARACTERS
        from shim_cli.session import remember
        from shim_cli.session.record import Record

        policy = load_policy()
        # The detector refuses more than this; a short count beats no count.
        note = "truncated" if len(text) > MAX_SOURCE_CHARACTERS else ""
        decision = evaluate(text[:MAX_SOURCE_CHARACTERS], policy.entities)
        remember(
            session_id,
            Record(
                client=client,
                event=_STOP_EVENT,
                tool_name="",
                direction="model-output",
                mode="observe",
                action="report",
                entities=tuple(decision.counts),
                in_bytes=len(text.encode("utf-8", "replace")),
                out_bytes=0,
                fields=1 if decision.counts else 0,
                note=note,
            ),
            _elapsed_ms(),
            policy.ledger,
        )
    except Exception:
        return


def _summary_output(session_id: str, stop_active: bool) -> bytes:
    if not session_id or stop_active:
        return b""
    try:
        import json

        from shim_cli.session import spool, summary

        records = spool.entries(session_id)
        if len(records) <= spool.summarized(session_id):
            return b""
        text = summary.render(records, spool.capped(session_id))
        spool.mark_summarized(session_id, len(records))
        if not text:
            return b""
        return json.dumps({"systemMessage": text}, ensure_ascii=False).encode()
    except Exception:
        return b""


def _forget(session_id: str) -> bytes:
    _sweep_suggestions()
    try:
        from shim_cli.session import spool

        spool.clear(session_id)
    except Exception:
        pass
    return b""


def _uninspected(raw: bytes, client: str, event: str, session_id: str) -> None:
    try:
        import json

        from shim_cli.session import remember
        from shim_cli.session.record import (
            NOT_INSPECTED,
            UNKNOWN_TOOL_LABEL,
            UNSUPPORTED_EVENT_LABEL,
            Record,
            display_label,
        )

        tool = ""
        try:
            document = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(document, dict) and isinstance(
                document.get("tool_name"), str
            ):
                tool = document["tool_name"]
        except Exception:
            pass
        event_label = event if event in _TOOL_EVENT_NAMES else UNSUPPORTED_EVENT_LABEL
        tool_label = display_label(tool, UNKNOWN_TOOL_LABEL)
        if tool_label != UNKNOWN_TOOL_LABEL:
            try:
                from shim_cli.guard import evaluate

                decision = evaluate(tool_label)
                if decision.counts:
                    tool_label = display_label(
                        decision.redacted_text, UNKNOWN_TOOL_LABEL
                    )
            except Exception:
                tool_label = UNKNOWN_TOOL_LABEL
        remember(
            session_id,
            Record(
                client=client,
                event=event_label,
                tool_name=tool_label,
                direction="",
                mode="",
                action="report",
                note=f"{NOT_INSPECTED}: analysis failed; passed through unchanged",
            ),
            _elapsed_ms(),
            _policy_ledger(),
        )
    except Exception:
        pass


def _policy_ledger() -> bool:
    try:
        from shim_cli.config import load_policy

        return load_policy().ledger
    except Exception:
        return False


def _tool_output(
    raw: bytes | dict[str, object], entry, event: str, session_id: str
) -> bytes:
    from shim_cli.config import load_policy
    from shim_cli.events.pipeline import process
    from shim_cli.guard import evaluate
    from shim_cli.session import remember

    policy = load_policy()

    def mode_for(direction: str, tool: str) -> str:
        return policy.mode_for(direction, tool, event)

    def entities_for(tool: str, _event: str = "") -> tuple:
        return policy.entities_for(tool, event)

    outcome = process(entry, raw, mode_for, evaluate, policy.diet, entities_for)
    remember(session_id, outcome.record, _elapsed_ms(), policy.ledger)
    return outcome.output


def _output(raw: bytes, client: str = "codex") -> bytes:
    if len(raw) > MAX_INPUT_BYTES:
        return _refusal_output(raw, client)

    try:
        with _silence_dependencies():
            tool_event_adapters = None
            if client == "codex":
                from shim_cli.clients.codex.hook import (
                    block_output,
                    error_output,
                    parse_input,
                    warn_output,
                )
            elif client == "claude":
                from shim_cli.clients.claude.hook import (
                    block_output,
                    error_output,
                    parse_input,
                    warn_output,
                )
                from shim_cli.clients.claude.tool_events import TOOL_EVENTS

                tool_event_adapters = TOOL_EVENTS
            elif client == "copilot":
                from shim_cli.clients.copilot.hook import (
                    block_output,
                    error_output,
                    parse_input,
                    warn_output,
                )
            else:
                return _error_output(client)

            try:
                try:
                    from shim_cli.clients.user_prompt_hook import parse_object

                    document = parse_object(raw)
                    event, session_id, stop_active = _envelope(document)
                except ValueError:
                    return _refusal_output(raw, client)
                if event == _STOP_EVENT:
                    _count_model_output(document, client, session_id)
                    return _summary_output(session_id, stop_active)
                if event == _SESSION_END_EVENT:
                    return _forget(session_id)
                if event and event not in _PROMPT_EVENTS:
                    if tool_event_adapters is None:
                        return b""
                    entry = tool_event_adapters.get(event)
                    if entry is None:
                        return b""
                    try:
                        return _tool_output(document, entry, event, session_id)
                    except Exception:
                        _uninspected(raw, client, entry.event, session_id)
                        return _tool_error_output(client, entry.event)

                prompt = parse_input(document)
                from shim_cli.config import load_policy
                from shim_cli.guard import evaluate
                from shim_cli.session import remember

                policy = load_policy()
                decision = evaluate(prompt, policy.entities)
                mode = policy.mode_for("user-prompt", event=event or _PROMPT_EVENT)

                def keep(action: str) -> None:
                    try:
                        record = _prompt_record(
                            client, event, mode, action, decision, prompt
                        )
                    except Exception:
                        return
                    remember(session_id, record, _elapsed_ms(), policy.ledger)

                if not decision.blocked:
                    keep("allow")
                    return b""
                if mode == "observe":
                    keep("allow")
                    return b""
                if client == "copilot":
                    keep("mask")
                    return warn_output(decision)
                if mode != "enforce":
                    keep("report")
                    return warn_output(decision)
                suggestion_path = _write_redacted_prompt(decision.redacted_text)
                try:
                    output = block_output(decision, suggestion_path)
                except Exception:
                    with contextlib.suppress(OSError):
                        Path(suggestion_path).unlink()
                    raise
                keep("deny")
                return output
            except Exception:
                return error_output()
    except Exception:
        return _refusal_output(raw, client)


def main() -> None:
    arguments = sys.argv[1:]
    client = "codex" if not arguments else arguments[0]
    if len(arguments) > 1:
        client = ""
    raw = b""
    try:
        with _deadline():
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            output = _output(raw, client)
        sys.stdout.buffer.write(output)
    except Exception:
        with contextlib.suppress(Exception):
            sys.stdout.buffer.write(_refusal_output(raw, client))


if __name__ == "__main__":
    main()
