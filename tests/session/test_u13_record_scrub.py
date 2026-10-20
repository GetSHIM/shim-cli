"""U13: what a session record keeps of a path or a tool name is scrubbed with
the user's whole policy, never with a reveal, on every path that writes one;
the prompt is scanned with the types its event is given."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from shim_cli.guard import evaluate
from shim_cli.session.record import scrubbed_target

ROOT = Path(__file__).parents[2]
SESSION = "u13-record-scrub"
CODENAME = "ACME-PROJECT-ZEBRA"
PLATE = "34 ABC 123"
IBAN = "TR330006100519786457841326"
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
RAW = (CODENAME, PLATE, IBAN, AWS_KEY)
# A secret folder, a project codename (custom), a plate (opt-in) and an IBAN
# whose last digits the settings reveal in content, never in a record.
FOLDER = f"/work/{CODENAME}/{PLATE}/{IBAN}"
SCRUBBED_FOLDER = "/work/<CUSTOM_1>/<TR_LICENSE_PLATE_1>/<IBAN_1>"
SETTINGS = (
    "ledger = true\n"
    'enabled_entities = ["EMAIL", "IBAN", "TR_LICENSE_PLATE", "CUSTOM"]\n'
    "[reveal]\nIBAN = 4\n"
    f'[[custom]]\nname = "PROJECT"\nliteral = "{CODENAME}"\n'
)
REVEALED = re.compile(r":[0-9]+>")


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    target = tmp_path / "settings" / "config.toml"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    target.chmod(0o600)
    monkeypatch.setenv("SHIM_CONFIG", str(target))


def _run(payload: dict | bytes, client: str = "claude") -> bytes:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-m", "shim_cli.hook", client),
        input=raw,
        capture_output=True,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        check=False,
        timeout=120,
    )
    assert (result.returncode, result.stderr) == (0, b"")
    return result.stdout


def _stored(root: Path) -> tuple[list, list]:
    """Every spool line and every ledger line, as text."""
    spool = [
        line
        for path in (root / "tmp").rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    ledger = [
        line
        for path in (root / "state").rglob("ledger-*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    return spool, ledger


def _assert_scrubbed(root: Path, *targets: str) -> None:
    spool, ledger = _stored(root)
    assert spool and ledger, "the spool and the ledger both hold the record"
    for line in spool + ledger:
        assert not any(value in line for value in RAW), line
        assert not REVEALED.search(line), line
    assert {json.loads(line)["target"] for line in spool + ledger} == set(targets)


@pytest.fixture
def _policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(tmp_path, monkeypatch, SETTINGS)


def test_a_tool_call_record_keeps_the_path_scrubbed_with_the_whole_policy(
    _policy, _isolated_roots: Path
) -> None:
    _run(
        {
            "hook_event_name": "PreToolUse",
            "session_id": SESSION,
            "permission_mode": "default",
            "tool_name": "Read",
            "tool_input": {"file_path": f"{FOLDER}/notes.txt"},
        }
    )

    _assert_scrubbed(_isolated_roots, f"{SCRUBBED_FOLDER}/notes.txt")


def test_an_uninspected_record_keeps_the_path_scrubbed_with_the_whole_policy(
    _policy, _isolated_roots: Path
) -> None:
    raw = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "session_id": SESSION,
            "permission_mode": "default",
            "tool_name": "Read",
            "tool_input": {"file_path": f"{FOLDER}/big.txt"},
            "tool_response": {"file": {"content": "x" * 1_100_000}},
        }
    ).encode()

    document = json.loads(_run(raw))

    assert "(too large)" in document["systemMessage"]
    _assert_scrubbed(_isolated_roots, f"{SCRUBBED_FOLDER}/big.txt")


def test_an_attachment_record_keeps_its_name_scrubbed_with_the_whole_policy(
    _policy, _isolated_roots: Path, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    folder = project / CODENAME / PLATE / IBAN
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("contact jane.doe@example.com\n", encoding="utf-8")
    name = f"{CODENAME}/{PLATE}/{IBAN}/a.txt"

    output = _run(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": SESSION,
            "permission_mode": "default",
            "cwd": str(project),
            "prompt": f'look at @"{name}"',
        }
    )

    assert not any(value.encode() in output for value in RAW)
    # The prompt's own record names no file; the attachment's names it scrubbed.
    _assert_scrubbed(
        _isolated_roots, "", "@<CUSTOM_1>/<TR_LICENSE_PLATE_1>/<IBAN_1>/a.txt"
    )


def test_unreadable_settings_still_scrub_an_uninspected_record(
    _isolated_roots: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, "ledgr = true\n")

    document = json.loads(
        _run(
            {
                "hook_event_name": "PreToolUse",
                "session_id": SESSION,
                "permission_mode": "default",
                "tool_name": "Read",
                "tool_input": {"file_path": f"/work/{AWS_KEY}/notes.txt"},
            }
        )
    )

    assert document["hookSpecificOutput"]["permissionDecision"] == "deny"
    spool, _ledger = _stored(_isolated_roots)
    [record] = [json.loads(line) for line in spool]
    assert record["note"] == "not inspected: settings unreadable"
    assert record["target"] == "/work/<SECRET_1>/notes.txt"


@pytest.mark.parametrize("separator", ("​", "\x01", "᠎", "؜", " ", "\x7f"))
def test_the_stored_target_is_the_text_that_was_scanned(separator: str) -> None:
    """Dropping a character the detector stopped at must not join a secret
    back together after the scan."""
    split = f"/w/AKIAIOSFODNN7{separator}EXAMPLE.txt"

    assert scrubbed_target(split, evaluate) == "/w/<SECRET_1>.txt"


def test_a_split_secret_in_a_tool_path_is_stored_masked(
    _isolated_roots: Path,
) -> None:
    _run(
        {
            "hook_event_name": "PostToolUse",
            "session_id": SESSION,
            "permission_mode": "default",
            "tool_name": "Read",
            "tool_input": {"file_path": "/w/AKIAIOSFODNN7\x01EXAMPLE.txt"},
            "tool_response": {"file": {"content": "hi"}},
        }
    )

    spool, _ledger = _stored(_isolated_roots)
    assert [json.loads(line)["target"] for line in spool] == ["/w/<SECRET_1>.txt"]


PROMPT_SCOPED = '[entities]\nUserPromptSubmit = ["EMAIL"]\n'
CARD = "4111 1111 1111 1111"


def test_entities_keyed_by_the_prompt_event_narrow_the_prompt_scan(
    _isolated_roots: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, PROMPT_SCOPED)
    project = tmp_path / "project"
    project.mkdir()
    (project / "card.txt").write_text(f"card {CARD}\n", encoding="utf-8")

    def prompt(text: str) -> bytes:
        return _run(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": SESSION,
                "permission_mode": "default",
                "cwd": str(project),
                "prompt": text,
            }
        )

    assert prompt(f"charge {CARD} and read @card.txt") == b""
    assert b"EMAIL (1)" in prompt("mail jane.doe@example.com")


# An MCP server name may carry a value: a revealed tail must not reach the
# record, and a type only the settings enable must be scrubbed there too.
MCP_TOOL = f"mcp__{IBAN}__read-{CODENAME}"
MCP_LABEL = "mcp__<IBAN_1>__read-<CUSTOM_1>"


def _assert_label(root: Path, label: str) -> None:
    spool, ledger = _stored(root)
    assert spool and ledger, "the spool and the ledger both hold the record"
    for line in spool + ledger:
        assert not any(value in line for value in RAW), line
        assert not REVEALED.search(line), line
    assert {json.loads(line)["tool_name"] for line in spool + ledger} == {label}


def test_an_mcp_tool_record_keeps_its_name_scrubbed_with_the_whole_policy(
    _policy, _isolated_roots: Path
) -> None:
    _run(
        {
            "hook_event_name": "PreToolUse",
            "session_id": SESSION,
            "permission_mode": "default",
            "tool_name": MCP_TOOL,
            "tool_input": {"query": "status"},
        }
    )

    _assert_label(_isolated_roots, MCP_LABEL)


def test_an_uninspected_mcp_record_keeps_its_name_scrubbed_with_the_whole_policy(
    _policy, _isolated_roots: Path
) -> None:
    raw = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "session_id": SESSION,
            "permission_mode": "default",
            "tool_name": MCP_TOOL,
            "tool_input": {"query": "status"},
            "tool_response": "x" * 1_100_000,
        }
    ).encode()

    output = _run(raw)

    assert b"(too large)" in output
    assert not any(value.encode() in output for value in RAW)
    _assert_label(_isolated_roots, MCP_LABEL)


def test_a_type_enabled_for_one_tool_is_scrubbed_from_its_record(
    _isolated_roots: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(
        tmp_path,
        monkeypatch,
        'ledger = true\nenabled_entities = ["EMAIL"]\n'
        '[entities]\nRead = ["TR_LICENSE_PLATE"]\n',
    )

    document = json.loads(
        _run(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "permission_mode": "default",
                "tool_name": "Read",
                "tool_input": {"file_path": f"/work/{PLATE}/a.txt"},
                "tool_response": {"file": {"content": f"plate {PLATE}"}},
            }
        )
    )

    assert PLATE not in json.dumps(document)
    _assert_scrubbed(_isolated_roots, "/work/<TR_LICENSE_PLATE_1>/a.txt")
