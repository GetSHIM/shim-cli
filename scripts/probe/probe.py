"""Capture, sanitize, and summarize client hook capability probes."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HOOK_EVENTS = (
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "Stop",
    "SessionEnd",
)
STEP_TIMEOUT_SECONDS = 300
_OPAQUE_ID = re.compile(
    r"\b(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|(?:toolu|msg|req|batch)_[A-Za-z0-9]{8,})\b"
)
_RESULT_FIELDS = ("tool_response", "toolResponse", "tool_result", "result", "output")

# Credentials are synthetic.
LARGE_FILE_FILLER = (
    "The quick brown fox jumps over the lazy dog while the build log scrolls. "
)
WORKSPACE_FILES = {
    "dotenv-sample.txt": (
        "DATABASE_URL=postgresql://alice:s3cr3tpw@db.example.com/app\n"
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
        "GITHUB_TOKEN=ghp_0123456789abcdefghijklmnopqrstuvwx\n"
        "password = SuperSecret123!\n"
        "OWNER_EMAIL=alice@example.com\n"
    ),
    "docker-compose.yml": (
        "services:\n"
        "  api:\n"
        "    image: example/api:1\n"
        "    environment:\n"
        "      DATABASE_URL: postgresql://alice:s3cr3tpw@db.example.com/app\n"
        "      REDIS_URL: redis://cache.example.com:6379/0\n"
    ),
    "notes.md": (
        "# Notes\n\n"
        "A password should never be shared in prose.\n"
        "Rotate the token and password regularly.\n"
    ),
}


@dataclass(frozen=True)
class Step:
    name: str
    prompt: str
    tools: tuple[str, ...]


STEPS = (
    Step(
        "read-large",
        "Use the Read tool on big.txt in the current directory, reading the "
        "whole file. Then reply with only the word DONE.",
        ("Read",),
    ),
    Step(
        "read-small",
        "Use the Read tool on dotenv-sample.txt. Then reply with only the word DONE.",
        ("Read",),
    ),
    Step(
        "bash-multiline",
        "Use the Bash tool to run exactly: cat dotenv-sample.txt && echo end. "
        "Then reply with only the word DONE.",
        ("Bash",),
    ),
    Step(
        "bash-failure",
        "Use the Bash tool to run exactly: cat no-such-file.txt. It will fail; "
        "that is expected. Then reply with only the word DONE.",
        ("Bash",),
    ),
    Step(
        "grep",
        "Use the Grep tool to search for the pattern password in the current "
        "directory. Then reply with only the word DONE.",
        ("Grep",),
    ),
    Step(
        "webfetch",
        "Use the WebFetch tool on https://example.com and summarise it in one "
        "word. Then reply with only the word DONE.",
        ("WebFetch",),
    ),
    Step(
        "mcp-echo",
        "Call the probe_echo MCP tool with customer_email set to "
        "alice@example.com and note set to ping. Then reply with only the "
        "word DONE.",
        ("mcp__probe__probe_echo",),
    ),
    Step(
        "write",
        "Use the Write tool to create secrets.txt whose entire content is the "
        "single line: password = SuperSecret123!. Then reply with only the "
        "word DONE.",
        ("Write",),
    ),
    Step(
        "bash-connection-string",
        "Use the Bash tool to run exactly: psql "
        "postgresql://alice:s3cr3tpw@db.example.com/app -c 'select 1'. "
        "It will fail; that is expected. Then reply with only the word DONE.",
        ("Bash",),
    ),
    Step(
        "model-reply",
        "First say only the word CHECKING. Then use the Read tool on "
        "dotenv-sample.txt. Then reply with one sentence naming the address "
        "that file sets as OWNER_EMAIL.",
        ("Read",),
    ),
    Step(
        "batch",
        "In one turn, use the Read tool on notes.md and the Read tool on "
        "docker-compose.yml. Then reply with only the word DONE.",
        ("Read",),
    ),
)


def build_workspace(root: Path, large_bytes: int = 60_000) -> Path:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    for name, content in WORKSPACE_FILES.items():
        (workspace / name).write_text(content, encoding="utf-8")
    repeats = -(-large_bytes // len(LARGE_FILE_FILLER))
    body = "".join(
        f"{index:05d} {LARGE_FILE_FILLER}\n" for index in range(1, repeats + 1)
    )
    (workspace / "big.txt").write_text(
        "Contact alice@example.com for access.\n" + body, encoding="utf-8"
    )
    return workspace


def hook_settings(command: list[str], timeout_seconds: int = 20) -> dict[str, object]:
    entry = {
        "hooks": [
            {
                "type": "command",
                "command": command[0],
                "args": command[1:],
                "timeout": timeout_seconds,
            }
        ]
    }
    return {
        "hooks": {event: [dict(entry, matcher="*")] for event in HOOK_EVENTS},
    }


def mcp_settings(command: list[str]) -> dict[str, object]:
    return {
        "mcpServers": {"probe": {"command": command[0], "args": command[1:], "env": {}}}
    }


def child_environment(
    capture_directory: str, system_message: str = ""
) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("CLAUDE")
    }
    environment.pop("SHIM_PROBE_SYSTEM_MESSAGE", None)
    environment["SHIM_PROBE_DIR"] = capture_directory
    if system_message:
        environment["SHIM_PROBE_SYSTEM_MESSAGE"] = system_message
    return environment


def run(
    client_binary: Path,
    root: Path,
    model: str,
    only: tuple[str, ...] = (),
    system_message: str = "",
) -> None:
    scripts = Path(__file__).resolve().parent
    workspace = build_workspace(root)
    settings_path = root / "probe-settings.json"
    mcp_path = root / "probe-mcp.json"
    capture_command = [sys.executable, str(scripts / "capture_hook.py")]
    server_command = [sys.executable, str(scripts / "mcp_echo_server.py")]
    _write_json(settings_path, hook_settings(capture_command))
    _write_json(mcp_path, mcp_settings(server_command))

    for step in STEPS:
        if only and step.name not in only:
            continue
        captures = root / "captures" / step.name
        captures.mkdir(parents=True, exist_ok=True)
        environment = child_environment(str(captures), system_message)
        command = [
            str(client_binary),
            "-p",
            step.prompt,
            "--settings",
            str(settings_path),
            "--mcp-config",
            str(mcp_path),
            "--strict-mcp-config",
            "--allowedTools",
            ",".join(step.tools),
            "--permission-mode",
            "bypassPermissions",
            "--model",
            model,
            "--effort",
            "low",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-hook-events",
            "--no-session-persistence",
        ]
        completed = subprocess.run(
            command,
            cwd=workspace,
            env=environment,
            capture_output=True,
            check=False,
            timeout=STEP_TIMEOUT_SECONDS,
        )
        (captures / "stream.jsonl").write_bytes(completed.stdout)
        (captures / "stderr.txt").write_bytes(completed.stderr)
        (captures / "exit-code.txt").write_text(
            str(completed.returncode), encoding="utf-8"
        )
        found = sum(1 for _ in captures.glob("*-*-*.json"))
        print(f"{step.name}: exit {completed.returncode}, {found} captures")


def mangle(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def replacements(root: Path) -> list[tuple[str, str]]:
    pairs = [
        (str(root.resolve()), "/probe"),
        (str(Path.home()), "/home/probe"),
        (sys.executable, "/usr/bin/python3"),
    ]
    pairs += [(mangle(needle), mangle(value)) for needle, value in pairs]
    return sorted(set(pairs), key=lambda pair: (-len(pair[0]), pair[0]))


def sanitize(
    value: object, pairs: list[tuple[str, str]], seen: dict[str, str]
) -> object:
    if isinstance(value, str):
        return _sanitize_text(value, pairs, seen)
    if isinstance(value, dict):
        return {
            _sanitize_text(str(key), pairs, seen): sanitize(item, pairs, seen)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item, pairs, seen) for item in value]
    return value


def _sanitize_text(
    text: str, pairs: list[tuple[str, str]], seen: dict[str, str]
) -> str:
    for needle, replacement in pairs:
        if needle:
            text = text.replace(needle, replacement)

    def substitute(match: re.Match[str]) -> str:
        original = match.group(0)
        if original not in seen:
            prefix, _, _ = original.partition("_")
            ordinal = len(seen) + 1
            seen[original] = (
                f"{prefix}_{ordinal:024d}"
                if "_" in original
                else f"00000000-0000-4000-8000-{ordinal:012d}"
            )
        return seen[original]

    return _OPAQUE_ID.sub(substitute, text)


def build_fixtures(root: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    pairs = replacements(root)
    seen: dict[str, str] = {}
    written: list[Path] = []
    for step_directory in sorted((root / "captures").glob("*")):
        if not step_directory.is_dir():
            continue
        grouped: dict[tuple[str, str], list[Path]] = {}
        for capture in sorted(step_directory.glob("*-*-*.json")):
            event, _, rest = capture.name.partition("-")
            tool = rest.rpartition("-")[0]
            grouped.setdefault((event, tool), []).append(capture)
        for (event, tool), captures in sorted(grouped.items()):
            for index, capture in enumerate(captures, start=1):
                payload = json.loads(capture.read_text(encoding="utf-8"))
                target = (
                    destination / f"{event}-{tool}-{step_directory.name}-{index}.json"
                )
                target.write_text(
                    json.dumps(
                        sanitize(payload, pairs, seen),
                        indent=2,
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                written.append(target)
    return written


def measure(payload: dict[str, object]) -> dict[str, object]:
    result_field = ""
    result_bytes = 0
    for name in _RESULT_FIELDS:
        if name in payload:
            result_field = name
            result_bytes = len(
                json.dumps(payload[name], ensure_ascii=False).encode("utf-8")
            )
            break
    return {
        "fields": sorted(payload),
        "result_field": result_field,
        "result_bytes": result_bytes,
        "total_bytes": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
    }


def summarize(directory: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            continue
        row: dict[str, object] = {"fixture": path.name}
        row.update(measure(payload))
        row["event"] = payload.get("hook_event_name", "")
        row["tool"] = payload.get("tool_name", "")
        rows.append(row)
    return rows


def _write_json(path: Path, document: dict[str, object]) -> None:
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    runner = commands.add_parser("run", help="drive a client through the probe")
    runner.add_argument("client_binary", type=Path)
    runner.add_argument("--root", type=Path, required=True)
    runner.add_argument("--model", default="sonnet")
    runner.add_argument("--only", default="")
    runner.add_argument("--system-message", default="")

    fixtures = commands.add_parser("fixtures", help="sanitise captures")
    fixtures.add_argument("--root", type=Path, required=True)
    fixtures.add_argument("--destination", type=Path, required=True)

    summary = commands.add_parser("summary", help="report fixture measurements")
    summary.add_argument("--fixtures", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "run":
        only = tuple(name for name in args.only.split(",") if name)
        run(args.client_binary, args.root, args.model, only, args.system_message)
        return 0
    if args.command == "fixtures":
        for path in build_fixtures(args.root, args.destination):
            print(path)
        return 0
    print(json.dumps(summarize(args.fixtures), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
