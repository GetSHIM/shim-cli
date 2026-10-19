"""The hook answers before the client gives up on it, even when the machine is
too busy to run it.

A client kills a hook that outlives the timeout shim registers, and Claude
Code then treats the event as a hook error and carries on with it: the event
passes uninspected. So the fail-closed answer is only an answer if it arrives
in time, counted from when the client started the hook, not from when Python
got round to running it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from shim_cli.cli.integrations import _hook_fragment
from shim_cli.hook import HOOK_DEADLINE_SECONDS

ROOT = Path(__file__).resolve().parents[2]
# What the hook must leave between its answer and the shortest client timeout,
# for the launcher before it, the write and the exit (docs/privacy.md).
MARGIN_SECONDS = 5
DENIED_CALL = (
    b'{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
    b'"permissionDecision":"deny","permissionDecisionReason":'
    b'"shim: could not inspect this tool call, so it was not allowed."}}'
)
READ_CALL = json.dumps(
    {
        "hook_event_name": "PreToolUse",
        "session_id": "u26",
        "permission_mode": "default",
        "tool_name": "Read",
        "tool_input": {"file_path": "/work/notes.md"},
    }
).encode()

MCP_RESULT = json.dumps(
    {
        "hook_event_name": "PostToolUse",
        "session_id": "u26",
        "permission_mode": "default",
        "tool_name": "mcp__db__query",
        "tool_input": {"sql": "select 1"},
        "tool_response": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
    }
).encode()
WITHHELD_PAST_DEADLINE = (
    b'{"systemMessage":"shim: withheld this mcp__db__query result; it could '
    b'not be inspected (deadline).","hookSpecificOutput":{"hookEventName":'
    b'"PostToolUse","updatedToolOutput":[{"type":"text","text":'
    b'"[withheld by shim]"}],"additionalContext":"shim withheld this '
    b"mcp__db__query result because it could not be inspected (deadline). "
    b"It is not empty; do not guess what it held, and ask the user how to "
    b'continue."}}'
)


def _timeouts(node: object):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("timeout", "timeoutSec"):
                yield value
            else:
                yield from _timeouts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _timeouts(item)


def _shortest_client_timeout() -> int:
    """Every timeout shim registers: what `shim install` writes for each client,
    and the plugin's hook files for Claude Code, Codex, VS Code and Copilot."""
    documents = [_hook_fragment(client) for client in ("claude", "codex", "copilot")]
    plugin_files = sorted((ROOT / "plugins").glob("*/**/hooks/*.json"))
    assert len(plugin_files) == 3, plugin_files
    documents += [json.loads(path.read_text()) for path in plugin_files]
    found = [value for document in documents for value in _timeouts(document)]
    assert found and all(type(value) is int for value in found), found
    return min(found)


def _environment(tmp_path: Path) -> dict:
    home, temporary = tmp_path / "home", tmp_path / "tmp"
    home.mkdir()
    temporary.mkdir()
    return {**os.environ, "HOME": str(home), "TMPDIR": str(temporary)}


def test_the_answer_is_due_well_inside_every_client_timeout() -> None:
    from shim_cli import hook

    assert hook.HOOK_DEADLINE_SECONDS < hook.HOOK_ANSWER_SECONDS
    assert hook.HOOK_ANSWER_SECONDS + MARGIN_SECONDS <= _shortest_client_timeout()


def test_the_age_counts_from_when_the_process_started(tmp_path: Path) -> None:
    """The kernel's start time, read the way this platform keeps it: a
    process that slept before asking is at least that old, and no older than
    the parent saw it live, but for the grain of the kernel's clock. Linux
    keeps the start in clock ticks, rounded down, so the age can read up to a
    tick older, never younger; macOS keeps microseconds."""
    slept = 1.5
    grain = 1e-6 if sys.platform == "darwin" else 1 / os.sysconf("SC_CLK_TCK")
    code = (
        "import time\n"
        f"time.sleep({slept})\n"
        "from shim_cli.hook import _process_age\n"
        "print(_process_age())\n"
    )

    result, elapsed = _hook(code, b"", tmp_path)

    assert (result.returncode, result.stderr) == (0, b"")
    assert slept <= float(result.stdout) <= elapsed + grain


def test_a_hook_held_up_before_python_runs_still_refuses_in_time(
    tmp_path: Path,
) -> None:
    """The hook as `shim install claude` registers it, given its event and
    then stopped past its deadline before Python runs, as a starved CPU or a
    slow disk holds up an interpreter's start. Counted from when it started,
    there is no time left to inspect: the MCP result must be withheld, as a
    result is, not answered as if it were a prompt, and in time."""
    process = subprocess.Popen(
        (sys.executable, "-I", "-B", "-m", "shim_cli.hook", "claude"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
        env=_environment(tmp_path),
    )
    started = time.monotonic()
    os.kill(process.pid, signal.SIGSTOP)
    try:
        assert process.stdin is not None
        process.stdin.write(MCP_RESULT)
        process.stdin.close()
        time.sleep(started + HOOK_DEADLINE_SECONDS + 1 - time.monotonic())
        os.kill(process.pid, signal.SIGCONT)
        process.wait(timeout=60)
        elapsed = time.monotonic() - started
    finally:
        process.kill()
        process.wait()
    assert process.stdout is not None and process.stderr is not None

    assert (process.returncode, process.stderr.read()) == (0, b"")
    assert process.stdout.read() == WITHHELD_PAST_DEADLINE
    assert elapsed <= _shortest_client_timeout() - MARGIN_SECONDS, (
        f"answered {elapsed:.1f} s after the client started the hook"
    )


def _hook(code: str, raw: bytes, tmp_path: Path) -> tuple:
    started = time.monotonic()
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", code),
        input=raw,
        capture_output=True,
        cwd=ROOT,
        env=_environment(tmp_path),
        check=False,
        timeout=60,
    )
    return result, time.monotonic() - started


@pytest.mark.parametrize(
    "refusal", ("time.sleep(20)", "1 / 0"), ids=("overruns", "fails")
)
def test_a_refusal_that_goes_wrong_is_replaced_by_the_fixed_answer(
    refusal: str, tmp_path: Path
) -> None:
    """Past the deadline the hook still has a refusal to shape, and on a
    starved machine that can run late too, or it can fail. The fixed refusal
    is written in its place, once and whole, by the time the answer is due."""
    code = (
        "import sys, time\n"
        "from shim_cli import hook as runner\n"
        "sys.argv.append('claude')\n"
        "runner.HOOK_DEADLINE_SECONDS = 0.5\n"
        "runner.HOOK_ANSWER_SECONDS = 2\n"
        "runner._output = lambda *_arguments: time.sleep(20)\n"
        f"runner._refusal_output = lambda *_arguments: {refusal}\n"
        "runner.main()\n"
    )

    result, elapsed = _hook(code, READ_CALL, tmp_path)

    assert (result.returncode, result.stdout, result.stderr) == (0, DENIED_CALL, b"")
    assert elapsed < 4, f"answered after {elapsed:.1f} s"


def test_an_answer_in_time_is_never_followed_by_a_second(tmp_path: Path) -> None:
    """A safe call's answer is nothing at all; a process still alive when the
    answer falls due must not add the fixed refusal after it."""
    code = (
        "import sys, time\n"
        "from shim_cli import hook as runner\n"
        "sys.argv.append('claude')\n"
        "runner.HOOK_DEADLINE_SECONDS = 0.5\n"
        "runner.HOOK_ANSWER_SECONDS = 1\n"
        "runner.main()\n"
        "time.sleep(2)\n"
    )

    result, _elapsed = _hook(code, READ_CALL, tmp_path)

    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


def test_the_fixed_answer_is_not_cut_short_when_main_finishes(tmp_path: Path) -> None:
    """The answer fell due, and the fixed refusal is still being written when
    main's own refusal comes back: main must neither answer a second time nor
    exit under it."""
    code = (
        "import sys, time\n"
        "from shim_cli import hook as runner\n"
        "sys.argv.append('claude')\n"
        "runner.HOOK_DEADLINE_SECONDS = 0.5\n"
        "runner.HOOK_ANSWER_SECONDS = 1\n"
        "fixed = runner._fixed_refusal\n"
        "def starved(*arguments):\n"
        "    time.sleep(1)\n"
        "    return fixed(*arguments)\n"
        "def late(*_arguments):\n"
        "    time.sleep(1)\n"
        "    return b'{}'\n"
        "runner._fixed_refusal = starved\n"
        "runner._output = lambda *_arguments: time.sleep(20)\n"
        "runner._refusal_output = late\n"
        "runner.main()\n"
    )

    result, _elapsed = _hook(code, READ_CALL, tmp_path)

    assert (result.returncode, result.stdout, result.stderr) == (0, DENIED_CALL, b"")
