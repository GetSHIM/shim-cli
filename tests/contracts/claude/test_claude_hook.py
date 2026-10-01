from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMMAND = (sys.executable, "-I", "-B", "-m", "shim_cli.hook", "claude")
GENERIC_BLOCK = (
    b'{"decision":"block","reason":"shim could not inspect this prompt, '
    b'so it was withheld. Run `shim doctor claude` for the reason.",'
    b'"suppressOriginalPrompt":true}'
)
READ_INSTRUCTION = "Read this file and use its contents as my prompt: "
UNANALYSABLE = "\ufdfa" * 12_000


def _redaction_files(root):
    return [
        item
        for item in root.rglob("*")
        if item.is_file() and item.suffix not in (".jsonl", ".mark")
    ]


def _run(
    raw: bytes, tmp_path: Path, env_extra: dict | None = None
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["TMPDIR"] = str(tmp_path)
    environment.update(env_extra or {})
    return subprocess.run(
        COMMAND,
        input=raw,
        capture_output=True,
        cwd=ROOT,
        env=environment,
        check=False,
        timeout=60,
    )


def _payload(prompt: str) -> bytes:
    return json.dumps(
        {
            "session_id": "session",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/workspace",
            "permission_mode": "default",
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        },
        separators=(",", ":"),
    ).encode()


def test_claude_code_runner_allows_safe_prompts_silently(tmp_path: Path) -> None:
    result = _run(_payload("Explain merge sort."), tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


def test_a_prompt_with_a_percent_sign_that_is_not_an_escape_is_inspected(
    tmp_path: Path,
) -> None:
    prompt = "Why does WHERE name LIKE '%admin%' miss rows? echo %DATE% is fine."

    result = _run(_payload(prompt), tmp_path)

    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


ENFORCE_PROMPT = (
    'enabled_entities = ["EMAIL", "PHONE", "CREDIT_CARD", "IBAN", "IP_ADDRESS", '
    '"MAC_ADDRESS", "US_SSN", "TR_NATIONAL_ID", "TR_VKN", "SECRET", "DB_URI"]\n'
    "\n[mode]\n"
    'user-prompt = "enforce"\n'
)


def test_claude_code_runner_reports_a_finding_and_lets_the_prompt_through(
    tmp_path: Path,
) -> None:
    result = _run(_payload("Contact alice@example.com"), tmp_path)
    document = json.loads(result.stdout)

    assert result.returncode == 0
    assert result.stderr == b""
    assert "decision" not in document
    assert document["systemMessage"] == (
        "shim: found EMAIL (1) in your prompt. Not modified."
    )
    assert b"alice@example.com" not in result.stdout
    assert not _redaction_files(tmp_path), "warning must not write a redaction file"


def test_claude_code_runner_blocks_with_a_private_redaction(tmp_path: Path) -> None:
    settings = tmp_path / "enforce.toml"
    settings.write_text(ENFORCE_PROMPT, encoding="utf-8")
    result = _run(
        _payload("Contact alice@example.com"),
        tmp_path,
        env_extra={"SHIM_CONFIG": str(settings)},
    )
    document = json.loads(result.stdout)
    path = Path(document["reason"].split(READ_INSTRUCTION, 1)[1])

    assert result.returncode == 0
    assert result.stderr == b""
    assert document["decision"] == "block"
    assert document["suppressOriginalPrompt"] is True
    assert b"alice@example.com" not in result.stdout
    assert path.read_text() == "Contact <EMAIL_1>"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_claude_code_runner_fails_closed_on_invalid_input(tmp_path: Path) -> None:
    result = _run(b'{"hook_event_name":"UserPromptSubmit"}', tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (0, GENERIC_BLOCK, b"")


def test_a_long_prompt_inspected_only_in_part_is_withheld(tmp_path: Path) -> None:
    prompt = "\ufdfa" * 12_000 + "x" * 88_100 + "\nDB_PASSWORD=Synthetic-pass-0000"

    result = _run(_payload(prompt), tmp_path)

    assert (result.returncode, result.stdout, result.stderr) == (0, GENERIC_BLOCK, b"")


def test_a_long_korean_prompt_is_read_not_withheld(tmp_path: Path) -> None:
    sentence = "한국어 문장을 길게 붙여 넣습니다. 각 줄은 평범한 설명입니다.\n"

    result = _run(_payload((sentence * 3_000)[:110_000]), tmp_path)

    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


def test_claude_code_runner_does_not_block_a_truncated_tool_event(
    tmp_path: Path,
) -> None:
    result = _run(b'{"hook_event_name":"PostToolUse",', tmp_path)
    document = json.loads(result.stdout)

    assert result.returncode == 0
    assert result.stderr == b""
    assert "decision" not in document
    assert "could not be inspected" in document["systemMessage"]


def test_an_oversized_tool_event_passes_through_and_still_reaches_the_summary(
    tmp_path: Path,
) -> None:
    """Too large to inspect is still something that happened.

    The event was refused before anything was recorded, so the turn left no
    trace: the session summary under-counted, and the one thing the user needed
    to know — which read went uninspected — was the thing that went missing.
    """
    raw = json.dumps(
        {
            "session_id": "oversized",
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/work/big.txt"},
            "tool_response": {"content": "x" * 1_200_000},
        },
        separators=(",", ":"),
    ).encode()

    result = _run(raw, tmp_path)

    assert result.returncode == 0
    assert result.stderr == b""
    assert "could not be inspected" in json.loads(result.stdout)["systemMessage"]

    records = [
        json.loads(line)
        for path in tmp_path.rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["tool_name"] == "Read"
    # The record keeps the path; the summary is what shortens it to a name.
    assert records[0]["target"] == "/work/big.txt"
    assert records[0]["note"].startswith("not inspected")


def _read(tool: str, tool_input: dict, response: dict) -> bytes:
    return json.dumps(
        {
            "session_id": "contract",
            "hook_event_name": "PostToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "tool_response": response,
        },
        separators=(",", ":"),
    ).encode()


def test_a_masked_read_tells_the_model_and_records_what_it_always_did(
    tmp_path: Path,
) -> None:
    raw = _read(
        "Read",
        {"file_path": "/work/service/.env"},
        {
            "type": "text",
            "file": {
                "content": "SUPPORT_EMAIL=alice@example.com\n"
                "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
            },
        },
    )

    result = _run(raw, tmp_path)

    assert (result.returncode, result.stderr) == (0, b"")
    assert result.stdout == (
        b'{"hookSpecificOutput":{"hookEventName":"PostToolUse",'
        b'"updatedToolOutput":{"type":"text","file":{"content":'
        b'"SUPPORT_EMAIL=<EMAIL_1>\\nAWS_ACCESS_KEY_ID=<SECRET_1>\\n"}},'
        b'"additionalContext":"shim: masked EMAIL (1), SECRET (1) in Read. '
        b"Placeholders such as <EMAIL_1> stand for real values in the source; "
        b'the source does not contain placeholders."}}'
    )
    [line] = [
        line
        for path in tmp_path.rglob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    record = json.loads(line)
    for volatile in ("session_id", "latency_ms", "ts"):
        del record[volatile]
    assert record == {
        "client": "claude",
        "event": "PostToolUse",
        "tool_name": "Read",
        "target": "/work/service/.env",
        "direction": "inbound",
        "mode": "enforce",
        "action": "mask",
        "entities": {"EMAIL": 1, "SECRET": 1},
        "in_bytes": 114,
        "out_bytes": 96,
        "fields": 1,
        "transforms": [],
        "markers": [],
        "custom": {},
        "bare_numbers": 0,
        "note": "",
    }


def test_a_safe_read_is_still_zero_bytes(tmp_path: Path) -> None:
    raw = _read(
        "Read",
        {"file_path": "/work/service/readme.md"},
        {"type": "text", "file": {"content": "nothing here\n"}},
    )

    result = _run(raw, tmp_path)

    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


def test_a_compacted_result_says_nothing_to_the_model(tmp_path: Path) -> None:
    rows = json.dumps({"rows": [{"id": n} for n in range(4)]}, indent=4)
    raw = _read(
        "WebFetch",
        {"url": "https://example.com/rows"},
        {"type": "text", "content": rows},
    )

    result = _run(raw, tmp_path)

    assert (result.returncode, result.stderr) == (0, b"")
    assert result.stdout == (
        b'{"hookSpecificOutput":{"hookEventName":"PostToolUse",'
        b'"updatedToolOutput":{"type":"text","content":'
        b'"{\\"rows\\":[{\\"id\\":0},{\\"id\\":1},{\\"id\\":2},{\\"id\\":3}]}"}}}'
    )


def _failure(error: object) -> bytes:
    return json.dumps(
        {
            "session_id": "failed-command",
            "cwd": "/workspace",
            "permission_mode": "default",
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_input": {"command": "cat .env && cat missing-file"},
            "tool_use_id": "toolu_00000000000000000000000000",
            "error": error,
            "is_interrupt": False,
        },
        separators=(",", ":"),
    ).encode()


def _isolated(tmp_path: Path) -> dict:
    return {"SHIM_CONFIG": str(tmp_path / "absent" / "config.toml")}


def test_a_failed_command_that_printed_a_secret_is_reported_to_both(
    tmp_path: Path,
) -> None:
    error = (
        "Exit code 1\nAWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
        "cat: missing-file: No such file or directory"
    )

    result = _run(_failure(error), tmp_path, _isolated(tmp_path))
    spooled = b"".join(path.read_bytes() for path in tmp_path.rglob("*.jsonl"))

    assert (result.returncode, result.stderr) == (0, b"")
    assert result.stdout == (
        b'{"systemMessage":"shim: found SECRET (1) in a failed Bash. Claude Code '
        b'does not let this output be masked; the model has these values.",'
        b'"hookSpecificOutput":{"hookEventName":"PostToolUseFailure",'
        b'"additionalContext":"shim: the output of this failed Bash contained '
        b'SECRET (1). Do not repeat these values in replies, files or commands."}}'
    )
    assert b'"event": "PostToolUseFailure"' in spooled
    assert b'"action": "report"' in spooled
    assert b"AKIA" not in spooled
    assert b"missing-file" not in spooled


def test_a_failed_command_with_nothing_sensitive_says_nothing(tmp_path: Path) -> None:
    error = "Exit code 1\ncat: missing-file: No such file or directory"

    result = _run(_failure(error), tmp_path, _isolated(tmp_path))

    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


def test_a_failed_command_whose_error_is_not_text_is_reported_uninspected(
    tmp_path: Path,
) -> None:
    result = _run(_failure(5), tmp_path, _isolated(tmp_path))

    assert (result.returncode, result.stderr) == (0, b"")
    assert result.stdout == (
        b'{"systemMessage":"shim: this tool event could not be inspected and was '
        b'not modified."}'
    )


ATTACHED = (
    "Attached files reach the model unmasked; to have them masked, ask the "
    "agent to read the file instead."
)
FIXTURE_ENV = "API_TOKEN=0123456789abcdef-synthetic\nSUPPORT_EMAIL=ops@example.com\n"


def _attaching(prompt: str, cwd: Path) -> bytes:
    return json.dumps(
        {
            "session_id": "attachments",
            "cwd": str(cwd),
            "permission_mode": "default",
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        },
        separators=(",", ":"),
    ).encode()


def _workspace(tmp_path: Path, files: dict) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    for name, content in files.items():
        (work / name).write_text(content, encoding="utf-8")
    return work


def _mode(tmp_path: Path, mode: str) -> dict:
    settings = tmp_path / f"{mode}.toml"
    settings.write_text(f'[mode]\nuser-prompt = "{mode}"\n', encoding="utf-8")
    return {"SHIM_CONFIG": str(settings)}


def test_an_attached_file_with_findings_is_reported_before_it_is_sent(
    tmp_path: Path,
) -> None:
    work = _workspace(tmp_path, {"fixture.env": FIXTURE_ENV})

    result = _run(
        _attaching("@fixture.env explain", work), tmp_path, _isolated(tmp_path)
    )

    assert (result.returncode, result.stderr) == (0, b"")
    assert json.loads(result.stdout) == {
        "systemMessage": (f"shim: @fixture.env holds SECRET (1), EMAIL (1). {ATTACHED}")
    }


def test_the_prompt_sentence_comes_before_the_attachment_sentence(
    tmp_path: Path,
) -> None:
    work = _workspace(tmp_path, {"fixture.env": FIXTURE_ENV})

    result = _run(
        _attaching("mail alice@example.com about @fixture.env", work),
        tmp_path,
        _isolated(tmp_path),
    )

    assert json.loads(result.stdout)["systemMessage"] == (
        "shim: found EMAIL (1) in your prompt. Not modified. "
        f"shim: @fixture.env holds SECRET (1), EMAIL (1). {ATTACHED}"
    )


def test_enforce_stops_a_prompt_whose_attachment_holds_findings(
    tmp_path: Path,
) -> None:
    work = _workspace(tmp_path, {"fixture.env": FIXTURE_ENV})

    result = _run(
        _attaching("@fixture.env explain", work), tmp_path, _mode(tmp_path, "enforce")
    )

    assert json.loads(result.stdout) == {
        "decision": "block",
        "reason": (
            "shim blocked this prompt: @fixture.env holds SECRET (1), EMAIL (1). "
            f"{ATTACHED}"
        ),
        "suppressOriginalPrompt": True,
    }
    assert not list(tmp_path.glob("shim-redacted-*"))


def test_enforce_with_findings_in_both_writes_the_redacted_prompt(
    tmp_path: Path,
) -> None:
    work = _workspace(tmp_path, {"fixture.env": FIXTURE_ENV})

    result = _run(
        _attaching("mail alice@example.com about @fixture.env", work),
        tmp_path,
        _mode(tmp_path, "enforce"),
    )
    reason = json.loads(result.stdout)["reason"]
    path = Path(reason.split(READ_INSTRUCTION, 1)[1].split("\n", 1)[0])

    assert reason.startswith("shim blocked this prompt: EMAIL (1).\n")
    assert reason.endswith(f"\n@fixture.env holds SECRET (1), EMAIL (1). {ATTACHED}")
    assert path.read_text() == "mail <EMAIL_1> about @fixture.env"


@pytest.mark.parametrize("mode", ("warn", "observe"))
def test_a_clean_or_observed_attachment_says_nothing(mode: str, tmp_path: Path) -> None:
    files = {"clean.txt": "nothing here\n", "fixture.env": FIXTURE_ENV}
    work = _workspace(tmp_path, files)
    prompt = "@clean.txt" if mode == "warn" else "@fixture.env"

    result = _run(_attaching(prompt, work), tmp_path, _mode(tmp_path, mode))

    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


def test_past_three_files_the_sentence_counts_the_rest(tmp_path: Path) -> None:
    token = "API_TOKEN=0123456789abcdef-synthetic\n"
    work = _workspace(tmp_path, {f"{name}.env": token for name in "abcd"})

    result = _run(
        _attaching("@a.env @b.env @c.env @d.env", work), tmp_path, _isolated(tmp_path)
    )

    assert json.loads(result.stdout)["systemMessage"] == (
        "shim: @a.env holds SECRET (1); @b.env holds SECRET (1); "
        f"@c.env holds SECRET (1), and 1 more. {ATTACHED}"
    )


def _not_inspected(name: str) -> dict:
    return {
        "systemMessage": (
            f"shim: @{name} was not inspected. It reaches the model as it is."
        )
    }


@pytest.mark.parametrize("mode", ("warn", "enforce"))
def test_an_attachment_past_the_limits_is_reported_not_blocked(
    mode: str, tmp_path: Path
) -> None:
    work = _workspace(tmp_path, {f"f{index}.txt": "clean\n" for index in range(9)})
    prompt = " ".join(f"@f{index}.txt" for index in range(9))

    result = _run(_attaching(prompt, work), tmp_path, _mode(tmp_path, mode))

    assert json.loads(result.stdout) == _not_inspected("f8.txt")


@pytest.mark.parametrize("mode", ("warn", "enforce"))
def test_a_file_the_client_does_not_attach_says_nothing(
    mode: str, tmp_path: Path
) -> None:
    work = _workspace(tmp_path, {"big.log": FIXTURE_ENV + "a" * 262_144})

    result = _run(_attaching("@big.log", work), tmp_path, _mode(tmp_path, mode))

    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


@pytest.mark.parametrize("mode", ("warn", "observe", "enforce"))
def test_an_attachment_the_detector_cannot_analyse_is_reported_not_withheld(
    mode: str, tmp_path: Path
) -> None:
    work = _workspace(tmp_path, {"q.sql": UNANALYSABLE})

    result = _run(_attaching("@q.sql explain", work), tmp_path, _mode(tmp_path, mode))

    assert (result.returncode, result.stderr) == (0, b"")
    if mode == "observe":
        assert result.stdout == b""
    else:
        assert json.loads(result.stdout) == _not_inspected("q.sql")


def test_an_attachment_scanned_only_in_part_is_reported_as_not_inspected(
    tmp_path: Path,
) -> None:
    dump = UNANALYSABLE + "\n" + ("x" * 99 + "\n") * 1_500
    work = _workspace(tmp_path, {"dump.sql": dump})

    result = _run(_attaching("@dump.sql", work), tmp_path, _isolated(tmp_path))

    assert result.stdout.startswith(b"{")
    assert json.loads(result.stdout) == _not_inspected("dump.sql")


def test_findings_in_the_scanned_part_of_an_attachment_are_still_reported(
    tmp_path: Path,
) -> None:
    dump = UNANALYSABLE + "\n" + ("x" * 99 + "\n") * 1_500 + FIXTURE_ENV
    work = _workspace(tmp_path, {"dump.sql": dump})

    result = _run(_attaching("@dump.sql", work), tmp_path, _isolated(tmp_path))

    assert json.loads(result.stdout) == {
        "systemMessage": f"shim: @dump.sql holds SECRET (1), EMAIL (1). {ATTACHED}"
    }


def test_attachments_past_the_time_budget_are_reported_not_scanned(
    tmp_path: Path,
) -> None:
    work = _workspace(tmp_path, {"fixture.env": FIXTURE_ENV})
    code = (
        "import sys\n"
        "from shim_cli import hook as runner\n"
        "sys.argv.append('claude')\n"
        "runner.ATTACHMENT_SECONDS = 0\n"
        "runner.main()\n"
    )
    environment = {**os.environ, "TMPDIR": str(tmp_path), **_isolated(tmp_path)}

    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", code),
        input=_attaching("@fixture.env", work),
        capture_output=True,
        cwd=ROOT,
        env=environment,
        check=False,
        timeout=60,
    )

    assert (result.returncode, result.stderr) == (0, b"")
    assert json.loads(result.stdout) == _not_inspected("fixture.env")


def test_an_attachment_is_recorded_by_name_without_its_values(
    tmp_path: Path,
) -> None:
    work = _workspace(tmp_path, {"fixture.env": FIXTURE_ENV})

    _run(_attaching("@fixture.env explain", work), tmp_path, _isolated(tmp_path))
    lines = [
        json.loads(line)
        for path in tmp_path.rglob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    spooled = "\n".join(path.read_text() for path in tmp_path.rglob("*.jsonl"))

    attachment = [line for line in lines if line["target"] == "@fixture.env"]
    assert [(line["action"], line["entities"]) for line in attachment] == [
        ("report", {"EMAIL": 1, "SECRET": 1})
    ]
    assert "0123456789abcdef" not in spooled
    assert "ops@example.com" not in spooled
