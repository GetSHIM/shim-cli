from __future__ import annotations

import contextlib
import os
import signal
import sys
import tempfile
import threading
import time
import warnings
from collections.abc import Iterator
from pathlib import Path

import shim_cli

MAX_INPUT_BYTES = 1_000_000
_PROMPT_EVENT = "UserPromptSubmit"
_PROMPT_EVENTS = frozenset({_PROMPT_EVENT, "userPromptTransformed"})
_STOP_EVENT = "Stop"
_SESSION_END_EVENT = "SessionEnd"
_STARTED = time.perf_counter()
# Counted from when the client started the hook process. Inspection stops at
# the deadline and the event is refused or withheld; if even that refusal is
# not out when the answer is due, a fixed one is. The shortest timeout shim
# registers with a client is 30 s: the 5 s left are for the launcher, the
# write and the exit (docs/privacy.md).
HOOK_DEADLINE_SECONDS = 20
HOOK_ANSWER_SECONDS = 25
ATTACHMENT_SECONDS = 15
WINDOWS_NOTICE = "shim: shim-cli does not support Windows yet; nothing was inspected.\n"
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
_VSCODE_ERROR_OUTPUT = (
    b'{"continue":false,"stopReason":"shim could not inspect this prompt, '
    b"so it was withheld. Open the shim plugin in VS Code to see whether it is "
    b'loaded, and check your shim settings."}'
)


def _error_output(client: str) -> bytes:
    if client == "claude":
        return _CLAUDE_ERROR_OUTPUT
    if client == "copilot":
        return _COPILOT_ERROR_OUTPUT
    if client == "vscode":
        return _VSCODE_ERROR_OUTPUT
    return _ERROR_OUTPUT


# For when not even the code shim inspects with loads: the answers its
# adapters give, with neither the tool nor the reason, and the reasons in the
# pipeline's own words, since that module may be the one that failed.
_TOO_LARGE = "too large"
_ANALYSIS_FAILED = "analysis failed"
_DENIED_CALL = (
    b'{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
    b'"permissionDecision":"deny","permissionDecisionReason":'
    b'"shim: could not inspect this tool call, so it was not allowed."}}'
)
_WITHHELD_RESULT = (
    b'{"systemMessage":"shim: withheld this tool result; it could not be '
    b'inspected.","hookSpecificOutput":{"hookEventName":"PostToolUse",'
    b'"updatedToolOutput":[{"type":"text","text":"[withheld by shim]"}],'
    b'"additionalContext":"shim withheld this tool result because it could not '
    b"be inspected. It is not empty; do not guess what it held, and ask the "
    b'user how to continue."}}'
)
_UNINSPECTED_OUTPUT = (
    b'{"systemMessage":"shim: could not inspect this tool output; the model has '
    b'it as it was.","hookSpecificOutput":{"hookEventName":"%s",'
    b'"additionalContext":"shim could not inspect this tool output. Do not '
    b'repeat values from it in replies, files or commands."}}'
)


def _tool_entry(client: str, event: str):
    if client == "vscode":
        from shim_cli.clients.vscode.tool_events import TOOL_EVENTS
    elif client == "claude":
        from shim_cli.clients.claude.tool_events import TOOL_EVENTS
    else:
        return None
    return TOOL_EVENTS.get(event)


def _reason(error: BaseException) -> str:
    try:
        from shim_cli.events.pipeline import failure_reason

        return failure_reason(error)
    except Exception:
        return _ANALYSIS_FAILED


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


_FILE_KEYS = ("file_path", "notebook_path", "path")


def _prefix_event(raw: bytes) -> dict[str, str]:
    """Event name, session id, tool name, target and client, from as much
    JSON as parses.

    Used when the document is too large or too broken for the real parser. The
    session id is what lets a refused event still reach the session summary,
    instead of the turn simply going missing from it; the client is "vscode"
    when the members read so far are VS Code's, as `_is_vscode_payload` tells.
    """
    import json

    text = raw.decode("utf-8", errors="replace")
    decoder = json.JSONDecoder()

    def skip_space(start: int) -> int:
        while start < len(text) and text[start] in " \t\r\n":
            start += 1
        return start

    event_name = session_id = camel_session = tool_name = target = ""
    settled = False
    members: dict = {}
    index = skip_space(0)
    if index < len(text) and text[index] == "{":
        index += 1
        while True:
            index = skip_space(index)
            if index == len(text) or text[index] == "}":
                break
            try:
                key, index = decoder.raw_decode(text, index)
            except (RecursionError, ValueError):
                break
            if not isinstance(key, str):
                break
            members[key] = None
            index = skip_space(index)
            if index == len(text) or text[index] != ":":
                break
            index = skip_space(index + 1)
            try:
                value, index = decoder.raw_decode(text, index)
            except (RecursionError, ValueError):
                break
            if key == "hook_event_name" and isinstance(value, str):
                # A prompt event name is decisive; a tool one only if nothing
                # better turns up. Neither ends the scan any more, because
                # `session_id` may still be ahead of us.
                if value in _PROMPT_EVENTS:
                    event_name, settled = value, True
                elif not settled and not event_name and value in _TOOL_EVENT_NAMES:
                    event_name = value
            elif key == "session_id" and isinstance(value, str):
                session_id = value
            elif key == "sessionId" and isinstance(value, str):
                camel_session = value
            elif key == "tool_name" and isinstance(value, str):
                tool_name = value
            elif key == "tool_input" and isinstance(value, dict):
                for name in _FILE_KEYS:
                    if isinstance(value.get(name), str):
                        target = value[name]
                        break
            index = skip_space(index)
            if index == len(text) or text[index] != ",":
                break
            index += 1
    return {
        "event": event_name,
        # As `_envelope` reads it: Copilot names the session in camel case.
        "session_id": session_id or camel_session,
        "tool_name": tool_name,
        "target": target,
        "client": "vscode" if _is_vscode_payload(members) else "",
    }


def _refused_envelope(raw: bytes) -> tuple[str, str, bool]:
    """Event, session, and whether VS Code sent it, for an event read no
    further."""
    try:
        from shim_cli.clients.user_prompt_hook import parse_object

        document = parse_object(raw)
        event, session, _stop = _envelope(document)
    except Exception:
        found = _prefix_event(raw)
        return found["event"], found["session_id"], found["client"] == "vscode"
    return event, session, _is_vscode_payload(document)


def _refusal_output(raw: bytes, client: str, reason: str = "") -> bytes:
    event, session, vscode = _refused_envelope(raw)
    if client == "claude" and vscode:
        # As `_output` does for an event it could read: VS Code runs the hook
        # installed for Claude Code, and cannot apply Claude Code's withhold.
        client = "vscode"
    if not event or event in _PROMPT_EVENTS:
        return _error_output(client)
    return _not_inspected(raw, client, event, session, reason)


def _fixed_refusal(raw: bytes, client: str) -> bytes:
    """`_refusal_output` for when it failed or is out of time: fixed bytes,
    picked from as much of the event as a quick read finds, with neither the
    detector nor the disk."""
    found = _prefix_event(raw)
    if client == "claude" and found["client"] == "vscode":
        client = "vscode"
    if not found["event"] or found["event"] in _PROMPT_EVENTS:
        return _error_output(client)
    return _fallback_output(raw, client, found["event"])


def _flush_standard_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.flush()


@contextlib.contextmanager
def _silence_dependencies() -> Iterator[None]:
    # By descriptor, not through sys: a hook started with a standard stream
    # closed has None there, and failing here skipped inspection altogether.
    # A descriptor that was closed stays on the null device afterwards.
    with open(os.devnull, "w", encoding="utf-8") as sink:
        saved = {}
        for descriptor in (1, 2):
            with contextlib.suppress(OSError):
                saved[descriptor] = os.dup(descriptor)
        try:
            _flush_standard_streams()
            for descriptor in (1, 2):
                os.dup2(sink.fileno(), descriptor)
            with (
                contextlib.redirect_stdout(sink),
                contextlib.redirect_stderr(sink),
                warnings.catch_warnings(),
            ):
                warnings.simplefilter("ignore")
                yield
        finally:
            _flush_standard_streams()
            for descriptor, copy in saved.items():
                os.dup2(copy, descriptor)
                os.close(copy)


def _process_age() -> float:
    """Seconds since this process started, as the kernel tells it, or 0 where
    it does not. A client's timeout runs from then, not from when Python, held
    up by a busy CPU or a slow disk, reached `main`."""
    try:
        if sys.platform == "darwin":
            import ctypes
            import struct

            # sysctl {CTL_KERN, KERN_PROC, KERN_PROC_PID, pid} fills a struct
            # kinfo_proc, which opens with the start time, a struct timeval.
            info = ctypes.create_string_buffer(1024)
            size = ctypes.c_size_t(len(info))
            name = (ctypes.c_int * 4)(1, 14, 1, os.getpid())
            failed = ctypes.CDLL(None).sysctl(
                name, 4, info, ctypes.byref(size), None, 0
            )
            if failed or size.value < 16:
                return 0.0
            seconds, microseconds = struct.unpack_from("@qi", info)
            return max(0.0, time.time() - seconds - microseconds / 1e6)
        with open("/proc/self/stat", "rb") as stat:
            # Field 22, counted after the parenthesised name: ticks since boot.
            ticks = int(stat.read().rsplit(b")", 1)[1].split()[19])
        booted = time.clock_gettime(time.CLOCK_BOOTTIME)
        return max(0.0, booted - ticks / os.sysconf("SC_CLK_TCK"))
    except Exception:
        return 0.0


@contextlib.contextmanager
def _deadline(started: float | None = None) -> Iterator[float]:
    """Arm the hook's alarm for HOOK_DEADLINE_SECONDS after `started`, a
    `perf_counter` reading (now if not given), and hand back when it is due.
    Already past it, arm nothing: the caller checks."""

    def expire(_signal_number: int, _frame: object) -> None:
        raise TimeoutError("shim hook deadline exceeded")

    if started is None:
        started = time.perf_counter()
    ends = started + HOOK_DEADLINE_SECONDS
    previous = signal.signal(signal.SIGALRM, expire)
    try:
        signal.setitimer(signal.ITIMER_REAL, max(0.0, ends - time.perf_counter()))
        yield ends
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


def _attachment_decision(text, policy, entities):
    from shim_cli.guard import evaluate

    if text is None or _elapsed_ms() > ATTACHMENT_SECONDS * 1000:
        return None
    try:
        found = evaluate(text, entities, policy.custom, policy.reveal)
    except Exception:
        return None
    return None if found.partial and not found.blocked else found


def _prompt_record(client, event, mode, action, decision, prompt, target=""):
    from shim_cli.session.record import Record

    return Record(
        client=client,
        event=event or _PROMPT_EVENT,
        tool_name="",
        target=target,
        direction="user-prompt",
        mode=mode,
        action=action,
        entities=tuple(decision.counts),
        in_bytes=len(prompt.encode("utf-8", "replace")),
        out_bytes=0,
        fields=1 if decision.counts else 0,
        custom=decision.custom_counts,
        bare_numbers=decision.bare_numbers,
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
        decision = evaluate(
            text[:MAX_SOURCE_CHARACTERS], policy.entities, policy.custom
        )
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
                custom=decision.custom_counts,
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
        from shim_cli import json_bytes
        from shim_cli.session import spool, summary

        records = spool.entries(session_id)
        if len(records) <= spool.summarized(session_id):
            return b""
        text = summary.render(records, spool.capped(session_id))
        spool.mark_summarized(session_id, len(records))
        if not text:
            return b""
        return json_bytes.dumps({"systemMessage": text})
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


def _described(source: bytes | dict, root: str = "", policy=None) -> tuple:
    """Tool label, target, the payload at `root` and the tool's own name,
    from as much as parses.

    Label and target reach the user and the summary, so both are scanned like
    any leaf; the payload and the name are handed to the encoder and never
    shown.
    """
    import json

    from shim_cli.session.record import (
        UNKNOWN_TOOL_LABEL,
        scrubbed_label,
        scrubbed_target,
    )

    document = source
    if isinstance(source, bytes):
        try:
            document = json.loads(source.decode("utf-8", "replace"))
        except Exception:
            document = None
    tool = target = ""
    payload = None
    if isinstance(document, dict):
        if isinstance(document.get("tool_name"), str):
            tool = document["tool_name"]
        given = document.get("tool_input")
        if isinstance(given, dict):
            for name in _FILE_KEYS:
                if isinstance(given.get(name), str):
                    target = given[name]
                    break
        if root:
            payload = document.get(root)
    if not tool and isinstance(source, bytes):
        # An oversized document arrives truncated at the read limit, so it
        # does not parse; the prefix is still enough to name the tool and the
        # file, which is what tells the user which read was skipped.
        found = _prefix_event(source)
        tool = found["tool_name"]
        target = target or found["target"]
    try:
        scrub = _record_scrub(policy)
        label = scrubbed_label(tool, scrub)
        target = scrubbed_target(target, scrub)
    except Exception:
        label, target = UNKNOWN_TOOL_LABEL, ""
    return label, target, payload, tool


def _uninspected(
    source: bytes | dict,
    client: str,
    event: str,
    session_id: str,
    reason: str,
    action: str,
    described: tuple,
    policy,
) -> None:
    try:
        from shim_cli.session import remember
        from shim_cli.session.record import (
            NOT_INSPECTED,
            UNSUPPORTED_EVENT_LABEL,
            Record,
        )

        tool, target, *_ = described or _described(source, "", policy)
        remember(
            session_id,
            Record(
                client=client,
                event=event if event in _TOOL_EVENT_NAMES else UNSUPPORTED_EVENT_LABEL,
                tool_name=tool,
                target=target,
                direction="",
                mode="",
                action=action,
                note=f"{NOT_INSPECTED}: {reason}",
            ),
            _elapsed_ms(),
            policy is not None and policy.ledger,
        )
    except Exception:
        pass


def _fallback_output(source: bytes | dict, client: str, event: str) -> bytes:
    """What the client is told when its adapter cannot answer: the call is
    denied, an MCP result withheld, and any other result reported."""
    if client not in ("claude", "vscode"):
        return b""
    if event == "PreToolUse":
        return _DENIED_CALL
    if (client, event) == ("claude", "PostToolUse"):
        name = (
            source.get("tool_name")
            if isinstance(source, dict)
            else _prefix_event(source)["tool_name"]
        )
        # As the adapter decides it: only a built-in result must keep a shape.
        if not isinstance(name, str) or not name or name.startswith("mcp__"):
            return _WITHHELD_RESULT
    if event in ("PostToolUse", "PostToolUseFailure"):
        return _UNINSPECTED_OUTPUT % event.encode()
    return b""


def _not_inspected(
    source: bytes | dict,
    client: str,
    event: str,
    session_id: str,
    reason: str = "",
    entry=None,
) -> bytes:
    """Fail closed as far as this event allows, and keep a record of it.

    A call is denied and a result withheld; where the client can do neither,
    the user and the model are told it was not inspected. Nothing in the
    answer or the record comes from the payload.
    """
    reason = reason or _ANALYSIS_FAILED
    policy = _settings()
    try:
        from shim_cli.events.pipeline import UNINSPECTED

        entry = entry or _tool_entry(client, event)
        described = _described(source, entry.root if entry else "", policy)
        label, _target, payload, name = described
        output = (
            entry.encode(UNINSPECTED, payload, reason, "", label, name)
            if entry
            else b""
        )
    except Exception:
        described = ()
        output = _fallback_output(source, client, event)
    if session_id:
        # Read off the answer itself, so the record cannot claim a withhold
        # the client was never asked for.
        withheld = (
            b'"updatedToolOutput"' in output or b'"permissionDecision":"deny"' in output
        )
        _uninspected(
            source,
            client,
            event,
            session_id,
            reason,
            "deny" if withheld else "report",
            described,
            policy,
        )
    return output


def _holds_hooks(path: Path, holds) -> bool:
    """Whether holds(content) is true of a client settings file. One that
    cannot be read or parsed counts, so that a write is never let through on
    a guess."""
    from shim_cli.clients.hook_settings import MAX_SETTINGS_BYTES

    try:
        # Non-blocking, so that a pipe in its place cannot hold the hook.
        with open(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            return holds(stream.read(MAX_SETTINGS_BYTES + 1))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True


def _control_files() -> list:
    """(file, hook file, holds) for each file a write can turn shim off with.
    shim's own settings have no test: they always count. A client's file
    counts while holds(its hook file's content) is true."""
    import json

    from shim_cli.clients.claude import settings as claude
    from shim_cli.clients.codex import settings as codex
    from shim_cli.clients.copilot import settings as copilot
    from shim_cli.config import config_path, legacy_config_path

    def installed(module):
        return lambda content: module.remove_hook(content) != content

    def claude_holds(content: bytes) -> bool:
        if claude.remove_hook(content) != content:
            return True
        # Or it switches on shim's marketplace plugin, which holds the hooks.
        document = json.loads(content)
        plugins = document.get("enabledPlugins") if isinstance(document, dict) else None
        return isinstance(plugins, dict) and any(
            name.split("@", 1)[0] in claude.PLUGIN_NAMES and enabled is not False
            for name, enabled in plugins.items()
        )

    files: list = [
        (path, None, None)
        for path in (config_path(), legacy_config_path())
        if path is not None
    ]
    files.append((claude.target_path(), claude.target_path(), claude_holds))
    for module in (codex, copilot):
        files.append((module.target_path(), module.target_path(), installed(module)))
    # Codex's own settings can switch its hooks off.
    files.append((codex.config_path(), codex.target_path(), installed(codex)))
    return files


def _plugin_folders() -> list:
    """The plugin folder running this hook, as Claude Code and VS Code name
    it: every file in it is part of shim's hooks. A variable naming a folder
    without shim's launcher is not shim's plugin, and is ignored."""
    folders = []
    for name in ("CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT"):
        folder = os.environ.get(name)
        if folder and os.path.isfile(os.path.join(folder, "hooks", "run-shim")):
            folders.append(os.path.join(os.path.realpath(folder), ""))
    return folders


def _fold(path: str) -> str:
    """A path as a case- and normalization-insensitive disk compares it, as
    macOS's default one does, even for a file that does not exist yet.
    Matching too much only refuses a write to a variant of a protected name."""
    import unicodedata

    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", path).casefold())


def _same_file(path: str, file: Path) -> bool:
    if _fold(path) == _fold(os.path.realpath(file)):
        return True
    try:
        return os.path.samefile(path, file)  # a hard link
    except OSError:
        return False


def _paths(text: str) -> list:
    """Each file a write tool's path can name: itself, and the file VS Code's
    edit tools write when they read it as a URI, which they do when it does
    not start with `/` and holds a `scheme:` anywhere (Copilot Chat 0.69.0,
    resolveFilePath). That is the URI's path, percent-decoded, from the root;
    `file://host/p` writes `//host/p`, which is `/host/p`.

    VS Code tests `\\w[\\w\\d+.-]*:\\S`. Before its colon, any match holds a
    word character followed only by `+.-`, so `\\w[+.-]*:\\S` finds the same
    strings, reading each character once. Searched as written it is quadratic,
    about an hour for a megabyte, and the alarm cannot interrupt it."""
    import re

    paths = [text]
    if not text.startswith("/") and re.search(r"\w[+.-]*:\S", text):
        from urllib.parse import unquote

        scheme, _colon, rest = text.partition(":")
        if not scheme or any(mark in scheme for mark in "/?#"):
            rest = text  # no scheme: the whole string is the path
        rest = rest.split("#", 1)[0].split("?", 1)[0]
        authority = ""
        if rest.startswith("//"):
            authority, _slash, rest = rest[2:].partition("/")
        paths.append(f"/{unquote(rest)}")
        if authority:
            paths.append(f"/{unquote(authority)}/{unquote(rest)}")
    return paths


def _protected(document: dict):
    """What `process` asks of each path a write tool names: the name of the
    file it would change, when that file can turn shim off, else "".

    Relative paths are the event's folder's, as the tool reads them, and
    links are followed on both sides. Client modules load on the first path.
    """
    cwd = document.get("cwd")
    folder = cwd if isinstance(cwd, str) else ""
    files: list = []
    plugins: list = []

    def protected(text: str) -> str:
        if not files:
            files.extend(_control_files())
            plugins.extend(_fold(plugin) for plugin in _plugin_folders())
        for given in _paths(text):
            path = os.path.realpath(os.path.join(folder, os.path.expanduser(given)))
            if _fold(path).startswith(tuple(plugins)):
                return os.path.basename(path)
            for file, hooks, holds in files:
                if _same_file(path, file) and (
                    holds is None or _holds_hooks(hooks, holds)
                ):
                    return file.name
        return ""

    return protected


def _issued(session_id: str) -> list:
    """The placeholder names shim issued in this session (D3)."""
    from shim_cli.session import spool

    return spool.issued(session_id) if session_id else []


def _issue(session_id: str, runs: tuple) -> None:
    """Keep the names of placeholders shim hands the model. Without a session
    there is nothing to keep them under, and nothing to check a write against."""
    if session_id and runs:
        from shim_cli.session import spool

        spool.issue(session_id, runs)


def _record_scrub(policy):
    """The one scan for what a record keeps of a path or a tool name: every
    type the settings name anywhere, every default type and the user's own
    patterns, and never a revealed digit. Without settings, the defaults."""
    from shim_cli.guard import evaluate
    from shim_cli.guard.entities import DEFAULT_ENTITIES

    if policy is None:
        return evaluate
    scoped = (name for names in policy.tool_entities.values() for name in names)
    entities = tuple(dict.fromkeys((*DEFAULT_ENTITIES, *policy.entities, *scoped)))
    return lambda text: evaluate(text, entities, policy.custom)


def _settings():
    """The settings, or None when they cannot be read. A record is scrubbed
    with them, or with the defaults; it never keeps a value raw."""
    try:
        from shim_cli.config import load_policy

        return load_policy()
    except Exception:
        return None


def _tool_output(
    raw: dict[str, object],
    entry,
    event: str,
    session_id: str,
    ends: float | None,
) -> bytes:
    from shim_cli.config import load_policy
    from shim_cli.events.pipeline import SETTINGS_UNREADABLE, process
    from shim_cli.guard import evaluate
    from shim_cli.guard.entities import DEFAULT_ENTITIES
    from shim_cli.session import remember

    try:
        policy = load_policy()
    except Exception:
        return _not_inspected(
            raw, entry.client, event, session_id, SETTINGS_UNREADABLE, entry
        )

    # The hook's own deadline, set by main before the read and the parse, and
    # checked here before every detector call rather than left to the alarm:
    # the detector re-arms that alarm on every call and each re-arm rounds up,
    # so across many small fields it goes off late or not at all. Called
    # without one, as a test does, the full budget from now.
    if ends is None:
        ends = time.perf_counter() + HOOK_DEADLINE_SECONDS

    def scan(text: str, entities: tuple = DEFAULT_ENTITIES, numbering=None):
        if time.perf_counter() > ends:
            raise TimeoutError("shim hook deadline exceeded")
        return evaluate(text, entities, policy.custom, policy.reveal, numbering)

    def mode_for(direction: str, tool: str) -> str:
        mode = policy.mode_for(direction, tool, event)
        if (
            mode == "enforce"
            and entry.power != "rewrite"
            and not policy.chose_mode(direction, tool, event)
        ):
            # Where masking is unavailable, enforcing refuses the call instead.
            # Nobody asked for that by installing; they get the report until
            # they ask for enforce themselves.
            return "warn"
        return mode

    def entities_for(tool: str, _event: str = "") -> tuple:
        return policy.entities_for(tool, event)

    outcome = process(
        entry,
        raw,
        mode_for,
        scan,
        policy.diet,
        entities_for,
        _protected(raw),
        lambda: _issued(session_id),
        lambda runs: _issue(session_id, runs),
        _record_scrub(policy),
    )
    remember(session_id, outcome.record, _elapsed_ms(), policy.ledger)
    if (
        policy.markers == "note"
        and outcome.record.markers
        and outcome.record.action != "deny"
        and (entry.client, entry.event) == ("claude", "PostToolUse")
    ):
        from shim_cli.clients.claude.tool_events import with_marker_note

        return with_marker_note(outcome.output, outcome.record.markers)
    return outcome.output


CLIENTS = ("claude", "codex", "copilot", "vscode")


def _is_vscode_payload(document: dict) -> bool:
    """True for an event VS Code sent to a hook installed as Claude Code's.

    VS Code reads `~/.claude/settings.json` by default, so `shim install
    claude` also runs inside VS Code — speaking a protocol VS Code does not
    share. Its prompt block would be ignored there and the prompt sent anyway,
    which is the one failure shim must never have: a user who asked for
    enforcement and silently did not get it.

    The two clients are told apart by what they always send. Every captured
    Claude Code event carries `permission_mode` and none carries `timestamp`;
    every captured VS Code event is the reverse.
    """
    return "timestamp" in document and "permission_mode" not in document


def _output(raw: bytes, client: str = "codex", ends: float | None = None) -> bytes:
    if client not in CLIENTS:
        # An older build meeting a newer plugin: with no adapter for that
        # client this hook cannot shape a refusal the client would understand,
        # and a guard that cannot run is off, not a reason to refuse the work.
        sys.stderr.write(
            f"shim: this build has no hook for {client or 'that client'}; "
            "nothing was inspected. Upgrade shim.\n"
        )
        return b""
    if len(raw) > MAX_INPUT_BYTES:
        return _refusal_output(raw, client, _TOO_LARGE)

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
            elif client == "vscode":
                from shim_cli.clients.vscode.hook import (
                    block_output,
                    error_output,
                    parse_input,
                    warn_output,
                )
                from shim_cli.clients.vscode.tool_events import (
                    TOOL_EVENTS as VSCODE_TOOL_EVENTS,
                )

                tool_event_adapters = VSCODE_TOOL_EVENTS
            else:  # pragma: no cover - _output refuses unknown clients first
                return b""

            try:
                try:
                    from shim_cli.clients.user_prompt_hook import parse_object

                    document = parse_object(raw)
                    event, session_id, stop_active = _envelope(document)
                except ValueError:
                    return _refusal_output(raw, client)
                if client == "claude" and _is_vscode_payload(document):
                    from shim_cli.clients.vscode.hook import (
                        block_output,
                        error_output,
                        parse_input,
                        warn_output,
                    )
                    from shim_cli.clients.vscode.tool_events import (
                        TOOL_EVENTS as VSCODE_TOOL_EVENTS,
                    )

                    tool_event_adapters = VSCODE_TOOL_EVENTS
                    client = "vscode"
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
                        return _tool_output(document, entry, event, session_id, ends)
                    except Exception as error:
                        return _not_inspected(
                            document,
                            entry.client,
                            event,
                            session_id,
                            _reason(error),
                            entry,
                        )

                prompt = parse_input(document)
                from shim_cli.config import load_policy
                from shim_cli.guard import evaluate
                from shim_cli.guard.evaluate import issued, numbering_for
                from shim_cli.session import remember

                policy = load_policy()
                entities = policy.entities_for("", event or _PROMPT_EVENT)
                # Held here only: the names the redacted prompt shows, for D3.
                numbering = numbering_for((prompt,))
                decision = evaluate(
                    prompt, entities, policy.custom, policy.reveal, numbering
                )
                if decision.partial:
                    return error_output()
                mode = policy.mode_for("user-prompt", event=event or _PROMPT_EVENT)
                files: list = []
                if client == "claude":
                    from shim_cli.clients.claude.hook import attached
                    from shim_cli.session.record import scrubbed_target

                    scrub = _record_scrub(policy)
                    files = [
                        (
                            scrubbed_target(name, scrub),
                            text,
                            _attachment_decision(text, policy, entities),
                        )
                        for name, text in attached(prompt, document.get("cwd"))
                    ]
                held = tuple(
                    (name, found.counts)
                    for name, _text, found in files
                    if found is not None and found.blocked
                )
                unread = tuple(name for name, _text, found in files if found is None)

                def keep(action: str) -> None:
                    from shim_cli.session.record import NOT_INSPECTED, Record

                    try:
                        records = [
                            _prompt_record(
                                client, event, mode, action, decision, prompt
                            ),
                            *(
                                _prompt_record(
                                    client, event, mode, action, found, text, f"@{name}"
                                )
                                for name, text, found in files
                                if found is not None and found.blocked
                            ),
                            *(
                                Record(
                                    client=client,
                                    event=event or _PROMPT_EVENT,
                                    tool_name="",
                                    target=f"@{name}",
                                    direction="user-prompt",
                                    mode=mode,
                                    action="report",
                                    note=f"{NOT_INSPECTED}: attachment",
                                )
                                for name in unread
                            ),
                        ]
                    except Exception:
                        return
                    for record in records:
                        remember(session_id, record, _elapsed_ms(), policy.ledger)

                if not (decision.blocked or held or unread) or mode == "observe":
                    keep("allow")
                    return b""
                if client == "copilot":
                    if decision.blocked:  # the model reads it rewritten
                        _issue(session_id, issued(numbering))
                    keep("mask")
                    return warn_output(decision)
                if mode != "enforce" or not (decision.blocked or held):
                    keep("report")
                    if not (held or unread):
                        return warn_output(decision)
                    from shim_cli.clients.claude.hook import (
                        warn_output as attached_report,
                    )

                    return attached_report(decision, held, unread)
                suggestion_path = None
                if decision.blocked:  # the redacted copy may be sent on
                    _issue(session_id, issued(numbering))
                    suggestion_path = _write_redacted_prompt(decision.redacted_text)
                try:
                    if held:
                        from shim_cli.clients.claude.hook import (
                            block_output as attached_block,
                        )

                        output = attached_block(decision, suggestion_path, held)
                    else:
                        output = block_output(decision, suggestion_path)
                except Exception:
                    if suggestion_path:
                        with contextlib.suppress(OSError):
                            Path(suggestion_path).unlink()
                    raise
                keep("deny")
                return output
            except Exception:
                return error_output()
    except Exception as error:
        return _refusal_output(raw, client, _reason(error))


def main() -> None:
    if shim_cli.WINDOWS:
        sys.stderr.write(WINDOWS_NOTICE)
        return
    arguments = sys.argv[1:]
    client = "codex" if not arguments else arguments[0]
    if len(arguments) > 1:
        client = ""
    started = time.perf_counter() - _process_age()
    raw = b""
    # Whoever takes this gives the one answer: main, or the timer below once
    # the answer is due. It is never released.
    answered = threading.Lock()

    def overdue(descriptor: int) -> None:
        if answered.acquire(blocking=False):
            try:
                answer = memoryview(_fixed_refusal(raw, client))
                while answer:
                    answer = answer[os.write(descriptor, answer) :]
            finally:
                os._exit(0)

    left = started + HOOK_ANSWER_SECONDS - time.perf_counter()
    if left > 0:
        with contextlib.suppress(OSError):
            # On a copy of standard output: inspection points descriptor 1 at
            # the null device.
            # ponytail: the timer needs the GIL, so a C call that holds it
            # delays the fixed answer by its length; the detector's calls on
            # its 200,000-character pieces are short. A watchdog process if not.
            timer = threading.Timer(left, overdue, (os.dup(1),))
            timer.daemon = True
            timer.start()
    try:
        with _deadline(started) as ends:
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            if time.perf_counter() >= ends:
                # Started past the deadline: read anyway, so the refusal fits
                # the event (a prompt's block would let an MCP result through).
                # The timer bounds that read; clients close stdin at once.
                # ponytail: started past the answer too, only the client's
                # kill bounds it; a start that late misses the timeout anyway.
                raise TimeoutError("shim hook deadline exceeded")
            output = _output(raw, client, ends)
    except Exception as error:
        try:
            output = _refusal_output(raw, client, _reason(error))
        except Exception:
            output = _fixed_refusal(raw, client)
    # Blocks for good if the timer has it: it is writing, and then exits.
    answered.acquire()
    with contextlib.suppress(Exception):
        sys.stdout.buffer.write(output)


if __name__ == "__main__":
    main()
