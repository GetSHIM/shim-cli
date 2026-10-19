"""D3: a write of a placeholder shim issued in this session is refused.

After a masked `Read`, the model holds `<SECRET_1>` where the file holds the
key. Writing what it saw back would put the placeholder over the real value on
disk. Every run gets a fresh home and temporary directory, and one session id
per test unless the test says otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMMAND = (sys.executable, "-I", "-B", "-m", "shim_cli.hook", "claude")
KEY = "AKIAIOSFODNN7EXAMPLE"
ADDRESS = "jane.doe@example.com"
SESSION = "u10-session"


class Hook:
    def __init__(self, tmp_path: Path, settings: str = "") -> None:
        self.home = tmp_path / "home"
        self.temporary = tmp_path / "tmp"
        self.home.mkdir()
        self.temporary.mkdir()
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("SHIM_", "XDG_", "CLAUDE_"))
        }
        self.environment.update(HOME=str(self.home), TMPDIR=str(self.temporary))
        if settings:
            config = tmp_path / "config.toml"
            config.write_text(settings, encoding="utf-8")
            config.chmod(0o600)
            self.environment["SHIM_CONFIG"] = str(config)

    def __call__(self, document: dict) -> bytes:
        result = subprocess.run(
            COMMAND,
            input=json.dumps(document).encode(),
            capture_output=True,
            cwd=ROOT,
            env=self.environment,
            check=False,
            timeout=60,
        )
        assert (result.returncode, result.stderr) == (0, b"")
        return result.stdout

    def disk(self) -> bytes:
        return b"".join(
            path.read_bytes() for path in self.temporary.rglob("*") if path.is_file()
        )


def _event(name: str, session: str = SESSION, **fields) -> dict:
    return {
        "session_id": session,
        "transcript_path": "/work/transcript.jsonl",
        "cwd": "/work",
        "permission_mode": "default",
        "hook_event_name": name,
        **fields,
    }


def _read(content: str, session: str = SESSION) -> dict:
    return _event(
        "PostToolUse",
        session,
        tool_name="Read",
        tool_input={"file_path": "/work/.env"},
        tool_response={
            "type": "text",
            "file": {"filePath": "/work/.env", "content": content},
        },
        tool_use_id="t1",
    )


def _call(tool: str, tool_input: dict, session: str = SESSION) -> dict:
    return _event(
        "PreToolUse", session, tool_name=tool, tool_input=tool_input, tool_use_id="t2"
    )


def _reason(output: bytes) -> str:
    specific = json.loads(output)["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    return specific["permissionDecisionReason"]


WRITES = (
    (
        "Write",
        {
            "file_path": "/work/.env",
            "content": "AWS_ACCESS_KEY_ID=<SECRET_1>\nPORT=6543\n",
        },
    ),
    (
        "Edit",
        {
            "file_path": "/work/.env",
            "old_string": "PORT=5432",
            "new_string": "KEY=<SECRET_1>\nPORT=6543",
        },
    ),
    (
        "MultiEdit",
        {
            "file_path": "/work/.env",
            "edits": [
                {"old_string": "PORT=5432", "new_string": "PORT=6543"},
                {"old_string": "DEBUG=true", "new_string": "KEY=<SECRET_1>"},
            ],
        },
    ),
    (
        "NotebookEdit",
        {"notebook_path": "/work/n.ipynb", "new_source": "key = '<SECRET_1>'"},
    ),
    ("mcp__files__write_file", {"path": "/work/.env", "text": "KEY=<SECRET_1>"}),
)


def _masked_read(hook: Hook, session: str = SESSION) -> None:
    output = hook(_read(f"AWS_ACCESS_KEY_ID={KEY}\nPORT=5432\n", session))
    updated = json.loads(output)["hookSpecificOutput"]["updatedToolOutput"]
    assert updated["file"]["content"] == "AWS_ACCESS_KEY_ID=<SECRET_1>\nPORT=5432\n"


@pytest.mark.parametrize(
    ("tool", "tool_input"), WRITES, ids=[tool for tool, _ in WRITES]
)
def test_a_write_of_a_placeholder_issued_in_this_session_is_refused(
    tmp_path: Path, tool: str, tool_input: dict
) -> None:
    hook = Hook(tmp_path)
    _masked_read(hook)

    reason = _reason(hook(_call(tool, tool_input)))

    assert reason == (
        f"shim: this {tool} call would write <SECRET_1>, a placeholder shim gave "
        "you in place of a real value, instead of that value. Change the real "
        "value another way, or ask the user."
    )
    assert KEY.encode() not in hook.disk()


@pytest.mark.parametrize("mode", ("observe", "warn", "enforce"))
def test_the_refusal_holds_in_every_mode(tmp_path: Path, mode: str) -> None:
    hook = Hook(tmp_path, f'[mode]\nlocal-write = "{mode}"\n')
    _masked_read(hook)

    _reason(hook(_call(*WRITES[0])))


def test_the_same_write_in_another_session_is_allowed(tmp_path: Path) -> None:
    hook = Hook(tmp_path)
    _masked_read(hook)

    assert hook(_call(*WRITES[0], session="another-session")) == b""


def test_a_placeholder_shim_never_issued_is_written_untouched(tmp_path: Path) -> None:
    hook = Hook(tmp_path)
    _masked_read(hook)

    assert (
        hook(_call("Write", {"file_path": "/work/doc.md", "content": "see <EMAIL_9>"}))
        == b""
    )


def test_a_placeholder_only_in_the_text_being_replaced_is_allowed(
    tmp_path: Path,
) -> None:
    hook = Hook(tmp_path)
    _masked_read(hook)

    edit = {
        "file_path": "/work/.env",
        "old_string": "KEY=<SECRET_1>",
        "new_string": "KEY=",
    }

    assert hook(_call("Edit", edit)) == b""


def test_a_write_without_placeholders_never_reads_the_session(tmp_path: Path) -> None:
    hook = Hook(tmp_path)

    assert (
        hook(_call("Write", {"file_path": "/work/a.txt", "content": "PORT=6543"}))
        == b""
    )


def test_the_issued_names_survive_a_full_session_log(tmp_path: Path) -> None:
    hook = Hook(tmp_path)
    assert hook(_read("PORT=5432\n")) == b""  # creates the session folder
    [log] = list(hook.temporary.rglob("*.jsonl"))
    filler = json.dumps({"note": "x" * 900}).encode() + b"\n"
    log.write_bytes(filler * (1_000_000 // len(filler)))
    _masked_read(hook)

    _reason(hook(_call(*WRITES[0])))


def test_the_end_of_a_session_forgets_its_names(tmp_path: Path) -> None:
    hook = Hook(tmp_path)
    _masked_read(hook)
    assert hook(_event("SessionEnd", reason="exit")) == b""

    assert not list(hook.temporary.rglob("*.names"))
    assert hook(_call(*WRITES[0])) == b""


def test_an_unreadable_name_store_refuses_a_placeholder_write(tmp_path: Path) -> None:
    hook = Hook(tmp_path)
    _masked_read(hook)
    [store] = [path for path in hook.temporary.rglob("*.names")]
    store.chmod(0o644)  # no longer private: shim will not trust it

    assert _reason(
        hook(_call("Write", {"file_path": "/work/a", "content": "<EMAIL_9>"}))
    ) == (
        "shim: could not check this Write call for placeholders shim gave you "
        "(session record unreadable), so it was not allowed."
    )
    assert hook(_call("Write", {"file_path": "/work/a", "content": "PORT=6543"})) == b""


def test_the_name_store_holds_names_and_nothing_from_the_values(tmp_path: Path) -> None:
    hook = Hook(tmp_path)
    hook(_read(f"AWS_ACCESS_KEY_ID={KEY}\nOWNER={ADDRESS}\nOTHER=bob@example.com\n"))

    [store] = [path for path in hook.temporary.rglob("*.names")]

    assert store.read_bytes() == b"EMAIL 1 2\nSECRET 1 1\n"
    for value in (KEY, ADDRESS, "bob@example.com", "example"):
        assert value.encode() not in hook.disk()


ENFORCE_PROMPT = '[mode]\nuser-prompt = "enforce"\n'


def test_a_prompt_redaction_issues_its_names_too(tmp_path: Path) -> None:
    hook = Hook(tmp_path, ENFORCE_PROMPT)
    blocked = json.loads(
        hook(_event("UserPromptSubmit", prompt=f"set owner to {ADDRESS}"))
    )
    assert blocked["decision"] == "block"

    reason = _reason(
        hook(_call("Write", {"file_path": "/work/o", "content": "<EMAIL_1>"}))
    )

    assert "<EMAIL_1>" in reason


def test_a_prompt_that_repeats_a_value_issues_only_the_one_name_shown(
    tmp_path: Path,
) -> None:
    hook = Hook(tmp_path, ENFORCE_PROMPT)
    hook(_event("UserPromptSubmit", prompt=f"owner {ADDRESS} cc {ADDRESS}"))

    [store] = [path for path in hook.temporary.rglob("*.names")]

    assert store.read_bytes() == b"EMAIL 1 1\n"
    assert hook(_call("Write", {"file_path": "/work/o", "content": "<EMAIL_2>"})) == b""
    assert "<EMAIL_1>" in _reason(
        hook(_call("Write", {"file_path": "/work/o", "content": "<EMAIL_1>"}))
    )


def test_a_prompt_names_above_its_own_literal_placeholder(tmp_path: Path) -> None:
    hook = Hook(tmp_path, ENFORCE_PROMPT)
    hook(_event("UserPromptSubmit", prompt=f"was <EMAIL_3>, now {ADDRESS}"))

    [store] = [path for path in hook.temporary.rglob("*.names")]

    assert store.read_bytes() == b"EMAIL 4 4\n"


def test_a_literal_placeholder_in_the_source_keeps_its_own_number(
    tmp_path: Path,
) -> None:
    hook = Hook(tmp_path)

    output = json.loads(
        hook(_read(f"Example output: <EMAIL_1>\nReal owner: {ADDRESS}\n"))
    )
    specific = output["hookSpecificOutput"]

    assert specific["updatedToolOutput"]["file"]["content"] == (
        "Example output: <EMAIL_1>\nReal owner: <EMAIL_2>\n"
    )
    assert specific["additionalContext"] == (
        "shim: masked EMAIL (1) in Read. Placeholders such as <EMAIL_1> stand "
        "for real values in the source."
    )
    # The literal one was never issued: writing it back is the model's own text.
    literal = {"file_path": "/work/doc.md", "content": "Example output: <EMAIL_1>"}
    assert hook(_call("Write", literal)) == b""
    assert "<EMAIL_2>" in _reason(
        hook(
            _call("Write", {"file_path": "/work/doc.md", "content": "owner <EMAIL_2>"})
        )
    )
