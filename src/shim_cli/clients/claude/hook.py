from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from shim_cli.clients import user_prompt_hook

if TYPE_CHECKING:
    from shim_cli.guard import GuardDecision

parse_input = user_prompt_hook.parse_input
warn_output = user_prompt_hook.warn_output

MAX_ATTACHMENTS = 8
MAX_ATTACHMENT_BYTES = 262_144
MAX_ATTACHED_BYTES = 1_000_000
_BINARY_PROBE_BYTES = 8_192
_START = r"(?:^|(?<=[\s。、？！]))@"
_QUOTED = re.compile(_START + r'"([^"]+)"')
# A bare mention is the client's `@(\S+)` cut back to a word boundary: from
# the first `@` of a run of non-space characters that starts the run or follows
# 。、？！, to the run's last ASCII word character. Read per run, as written
# here, because the regular expression retried every `@` of a run such as
# `。@。@...` against the rest of the run: 20,000 characters took 4 s.
_RUN = re.compile(r"\S+")
_RUN_AT = re.compile(r"(?:^|(?<=[。、？！]))@")
_TO_LAST_WORD = re.compile(r".*[A-Za-z0-9_]", re.DOTALL)
_LINES = re.compile(r"([^#]+)(?:#L(\d+)(?:-(\d+))?)?(?:#[^#]*)?")


def block_output(
    decision: GuardDecision, suggestion_path: str | None = None, held: tuple = ()
) -> bytes:
    return user_prompt_hook.block_output(
        decision, suggestion_path, suppress_original_prompt=True, held=held
    )


def error_output() -> bytes:
    return user_prompt_hook.error_output("claude", suppress_original_prompt=True)


def mentions(prompt: str) -> tuple[str, ...]:
    quoted = [
        match.group(1)
        for match in _QUOTED.finditer(prompt)
        if not match.group(1).endswith(" (agent)")
    ]
    bare = []
    for run in map(re.Match.group, _RUN.finditer(prompt)):
        at = _RUN_AT.search(run)
        word = at and _TO_LAST_WORD.match(run, at.end())
        if word and not run.startswith('"', at.end()):
            bare.append(word.group())
    return tuple(
        dict.fromkeys(
            mention for mention in quoted + bare if ":" not in mention.split("/", 1)[0]
        )
    )


def _read(path: str, limit: int) -> bytes | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except (OSError, ValueError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(limit + 1)
    finally:
        os.close(descriptor)


def attached(prompt: str, cwd: object) -> Iterator[tuple[str, str | None]]:
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return
    budget = MAX_ATTACHED_BYTES
    files = 0
    seen = set()
    for mention in mentions(prompt):
        found = _LINES.fullmatch(mention)
        name = found.group(1) if found else mention
        # A file and its lines are attached once however they are written;
        # past the count limit nothing is inspected, so only the file counts.
        lines = found.group(2, 3) if found and files < MAX_ATTACHMENTS else ()
        if (name, lines) in seen:
            continue
        seen.add((name, lines))
        content = _read(str(Path(cwd, os.path.expanduser(name))), MAX_ATTACHMENT_BYTES)
        if (
            content is None
            or len(content) > MAX_ATTACHMENT_BYTES
            or b"\0" in content[:_BINARY_PROBE_BYTES]
        ):
            continue
        files += 1
        if files > MAX_ATTACHMENTS or len(content) > budget:
            yield name, None
            continue
        budget -= len(content)
        text = content.decode("utf-8", errors="replace")
        if found and found.group(2):
            start = int(found.group(2))
            end = int(found.group(3) or start)
            lines = text.split("\n")
            text = "\n".join(
                lines[start - 1 : end] if start and end else lines[max(start - 1, 0) :]
            )
        yield name, text
