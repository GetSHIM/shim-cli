from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "probe"


def _module(name: str) -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        f"shim_probe_{name}", SCRIPTS / f"{name}.py"
    )
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


capture_hook = _module("capture_hook")
mcp_echo_server = _module("mcp_echo_server")
probe = _module("probe")


def _run_capture(payload: bytes, directory: Path | None) -> subprocess.CompletedProcess:
    environment = {"PATH": "/usr/bin:/bin"}
    if directory is not None:
        environment["SHIM_PROBE_DIR"] = str(directory)
    return subprocess.run(
        (sys.executable, "-I", "-B", str(SCRIPTS / "capture_hook.py")),
        input=payload,
        capture_output=True,
        check=False,
        env=environment,
        timeout=30,
    )


def test_capture_hook_stores_the_payload_verbatim_and_stays_silent(
    tmp_path: Path,
) -> None:
    payload = json.dumps(
        {"hook_event_name": "PostToolUse", "tool_name": "Read", "x": "ünïcode"}
    ).encode()
    result = _run_capture(payload, tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    written = sorted(tmp_path.glob("*.json"))
    assert len(written) == 1
    assert written[0].read_bytes() == payload
    assert written[0].name.startswith("PostToolUse-Read-")


def test_capture_hook_allows_the_event_when_no_capture_directory_is_set() -> None:
    result = _run_capture(b'{"hook_event_name":"PreToolUse"}', None)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


def test_capture_hook_records_unparsable_input_without_failing(tmp_path: Path) -> None:
    result = _run_capture(b"\xff\xfenot json", tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    written = sorted(tmp_path.glob("*.json"))
    assert len(written) == 1
    assert written[0].name.startswith("unparsed-unparsed-")
    assert written[0].read_bytes() == b"\xff\xfenot json"


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        ({"hook_event_name": "Stop"}, ("Stop", "none")),
        ({"hookEventName": "PreToolUse", "toolName": "Bash"}, ("PreToolUse", "Bash")),
        ({"hook_event_name": "x/../y", "tool_name": "a b"}, ("x____y", "a_b")),
        ({}, ("unnamed", "none")),
        ([], ("unparsed", "unparsed")),
    ),
)
def test_describe_reads_event_and_tool_names_safely(
    payload: object, expected: tuple[str, str]
) -> None:
    raw = json.dumps(payload).encode()
    assert capture_hook.describe(raw) == expected


def test_mcp_server_answers_the_full_handshake() -> None:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "probe_echo",
                "arguments": {"customer_email": "alice@example.com", "note": "ping"},
            },
        },
        {"jsonrpc": "2.0", "id": 4, "method": "nope"},
    ]
    source = io.StringIO("\n".join(json.dumps(item) for item in requests) + "\n")
    sink = io.StringIO()
    mcp_echo_server.serve(source, sink)
    responses = [json.loads(line) for line in sink.getvalue().splitlines()]

    assert [item["id"] for item in responses] == [1, 2, 3, 4]
    assert responses[0]["result"]["serverInfo"]["name"] == "shim-probe"
    assert responses[1]["result"]["tools"][0]["name"] == "probe_echo"
    assert json.loads(responses[2]["result"]["content"][0]["text"]) == {
        "customer_email": "alice@example.com",
        "note": "ping",
    }
    assert responses[3]["error"]["code"] == -32601


def test_sanitize_is_pure_deterministic_and_idempotent(tmp_path: Path) -> None:
    pairs = [(str(tmp_path), "/probe")]
    payload = {
        "session_id": "3F2504E0-4F89-41D3-9A0C-0305E82C3301",
        "cwd": f"{tmp_path}/workspace",
        "nested": [{"other": "6ba7b810-9dad-11d1-80b4-00c04fd430c8"}],
        "again": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
        "count": 3,
    }
    original = json.loads(json.dumps(payload))

    first = probe.sanitize(payload, pairs, {})
    second = probe.sanitize(payload, pairs, {})

    assert payload == original, "sanitize must not mutate its input"
    assert first == second, "sanitize must be deterministic"
    assert probe.sanitize(first, pairs, {}) == first, "sanitize must be idempotent"
    assert first["cwd"] == "/probe/workspace"
    assert first["session_id"] == "00000000-0000-4000-8000-000000000001"
    assert first["nested"][0]["other"] == "00000000-0000-4000-8000-000000000002"
    assert first["again"] == "00000000-0000-4000-8000-000000000003"
    assert first["count"] == 3


def test_build_fixtures_is_stable_across_runs(tmp_path: Path) -> None:
    captures = tmp_path / "captures" / "read-small"
    captures.mkdir(parents=True)
    for index, size in ((1, 10), (2, 20)):
        (captures / f"PostToolUse-Read-{index}.json").write_text(
            json.dumps(
                {
                    "hook_event_name": "PostToolUse",
                    "tool_name": "Read",
                    "cwd": str(tmp_path),
                    "tool_response": "x" * size,
                }
            ),
            encoding="utf-8",
        )
    destination = tmp_path / "fixtures"

    written = probe.build_fixtures(tmp_path, destination)
    snapshot = {path.name: path.read_bytes() for path in written}

    assert sorted(snapshot) == [
        "PostToolUse-Read-read-small-1.json",
        "PostToolUse-Read-read-small-2.json",
    ]
    leaked = snapshot["PostToolUse-Read-read-small-1.json"].decode()
    assert str(tmp_path) not in leaked

    again = probe.build_fixtures(tmp_path, destination)
    assert {path.name: path.read_bytes() for path in again} == snapshot


def test_summary_reports_fields_and_result_sizes(tmp_path: Path) -> None:
    (tmp_path / "PostToolUse-read-small-1.json").write_text(
        json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Read",
                "tool_response": {"file": {"content": "abc"}},
            }
        ),
        encoding="utf-8",
    )
    rows = probe.summarize(tmp_path)

    assert len(rows) == 1
    assert rows[0]["fields"] == ["hook_event_name", "tool_name", "tool_response"]
    assert rows[0]["result_field"] == "tool_response"
    assert rows[0]["result_bytes"] == len(b'{"file": {"content": "abc"}}')
    assert rows[0]["event"] == "PostToolUse"
    assert rows[0]["tool"] == "Read"


def test_hook_settings_register_every_probe_event_without_a_shell() -> None:
    document = probe.hook_settings(["/usr/bin/python3", "/probe/capture_hook.py"])
    assert sorted(document["hooks"]) == sorted(probe.HOOK_EVENTS)
    for event, groups in document["hooks"].items():
        assert groups[0]["matcher"] == "*", event
        entry = groups[0]["hooks"][0]
        assert entry["type"] == "command"
        assert entry["command"] == "/usr/bin/python3"
        assert entry["args"] == ["/probe/capture_hook.py"]


def test_workspace_files_carry_only_synthetic_values(tmp_path: Path) -> None:
    workspace = probe.build_workspace(tmp_path, large_bytes=2_000)
    body = (workspace / "big.txt").read_text(encoding="utf-8")

    assert len(body.encode()) > 2_000
    assert "alice@example.com" in body
    assert (workspace / "dotenv-sample.txt").read_text(
        encoding="utf-8"
    ) == probe.WORKSPACE_FILES["dotenv-sample.txt"]
    assert sorted(path.name for path in workspace.iterdir()) == [
        "big.txt",
        "docker-compose.yml",
        "dotenv-sample.txt",
        "notes.md",
        "page.html",
    ]


def test_the_probe_page_is_a_whole_page_of_about_40_kb() -> None:
    from shim_cli.events.html_text import HEADER, to_text

    page = probe.page_html()
    text = to_text(page).split("\n")

    assert 38_000 < len(page.encode()) < 42_000
    assert text[:5] == [
        HEADER,
        "Release status",
        "# Release status",
        "Questions go to ops@example.com.",
        "## Build history",
    ]
    assert text[-2:] == [
        "## Links",
        "Runbook (https://example.com/runbook) and "
        "dashboard (https://example.com/dashboard).",
    ]
    assert len("\n".join(text)) < len(page) // 2


def test_mcp_server_returns_a_named_page_as_one_text_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "page.html").write_text("<!doctype html><p>x</p>", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "probe_echo",
            "arguments": {"customer_email": "ops@example.com", "page": "../page.html"},
        },
    }

    response = mcp_echo_server.respond(request)

    assert response["result"]["content"] == [
        {"type": "text", "text": "<!doctype html><p>x</p>"}
    ]


def test_child_environment_drops_the_parent_session_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("PATH", "/usr/bin")

    environment = probe.child_environment("/probe/captures")

    assert not [key for key in environment if key.startswith("CLAUDE")]
    assert environment["PATH"] == "/usr/bin"
    assert environment["SHIM_PROBE_DIR"] == "/probe/captures"


def test_sanitize_neutralises_opaque_tool_identifiers() -> None:
    payload = {
        "tool_use_id": "toolu_01GPJfx75x23wy4Dhm7BNkSo",
        "same": "toolu_01GPJfx75x23wy4Dhm7BNkSo",
        "other": "msg_01ABCDEFGHIJKLMNOPQRSTUV",
    }
    result = probe.sanitize(payload, [], {})

    assert result["tool_use_id"] == "toolu_000000000000000000000001"
    assert result["same"] == result["tool_use_id"]
    assert result["other"] == "msg_000000000000000000000002"


def test_capture_hook_emits_a_system_message_only_when_asked(tmp_path: Path) -> None:
    result = subprocess.run(
        (sys.executable, "-I", "-B", str(SCRIPTS / "capture_hook.py")),
        input=b'{"hook_event_name":"Stop"}',
        capture_output=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "SHIM_PROBE_DIR": str(tmp_path),
            "SHIM_PROBE_SYSTEM_MESSAGE": "SENTINEL",
        },
        timeout=30,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"systemMessage": "SENTINEL"}
    assert result.stderr == b""


def test_child_environment_clears_an_inherited_system_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHIM_PROBE_SYSTEM_MESSAGE", "leaked")

    assert "SHIM_PROBE_SYSTEM_MESSAGE" not in probe.child_environment("/probe")
    assert (
        probe.child_environment("/probe", "asked")["SHIM_PROBE_SYSTEM_MESSAGE"]
        == "asked"
    )
