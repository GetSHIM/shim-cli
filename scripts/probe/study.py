"""PRD-18 R1: does the context diet break the provider's prompt cache?

The hypothesis is that it does not. The diet rewrites a tool result once, when
it enters the transcript; every later request in the session replays the
rewritten text, so the cached prefix is stable unless the setting changes
mid-session. This drives one scripted Claude Code session per configuration
through `shim watch` and records the per-request usage series the proxy reads
off the wire.

Two properties of the diet decide the shape of the task below, and getting
either wrong measures nothing:

  - it skips file-viewing tools (`Read` carries `file_path`), so a task made of
    reads is identical with the diet on and off;
  - its default transform is JSON compaction, so the result has to be
    pretty-printed JSON for there to be bytes to remove.

So the workspace holds four pretty-printed JSON files and the task reads them
through `Bash`, which the diet is allowed to rewrite.

    python scripts/probe/study.py --root /tmp/study --runs 2

Nothing here is part of the package, and no captured body is written to disk:
the harness keeps only the numbers `shim watch --json` already reports.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe import build_workspace  # noqa: E402

SESSION_TIMEOUT_SECONDS = 1200
FLIP_AFTER_SECONDS = 25

# One turn, ten tool exercises, so the requests inside a single session share a
# growing prefix. Separate `claude -p` runs would each start a cold cache.
# Every step has to produce a number the final answer needs: asked politely to
# "work through these steps", the model completed one of ten and stopped.
TASK = (
    "Do these ten steps in order, one tool call each. Keep a tally as you go. "
    "You must complete all ten; do not skip, batch or summarise early.\n"
    "1. Bash: cat data-1.json\n"
    "2. Bash: cat data-2.json\n"
    "3. Read notes.md\n"
    "4. Bash: cat data-3.json\n"
    "5. Bash: cat data-4.json\n"
    "6. Read dotenv-sample.txt\n"
    "7. Bash: cat data-1.json\n"
    "8. Bash: wc -l data-2.json\n"
    "9. Read dotenv-sample.txt\n"
    "10. Bash: cat data-3.json\n"
    "Finally, reply with one line per step in the form "
    "'<step number>: <number of lines that step returned>'. "
    "You cannot answer without having run every step.\n"
)

TOOLS = "Read,Bash"

# Steps 7 and 9 repeat an earlier target; PRD-18 R3 counts what that costs.
REPEATED_STEPS = (7, 9)

DIET_ON = 'enabled_entities = ["EMAIL", "IBAN", "SECRET", "DB_URI"]\n'
DIET_OFF = DIET_ON + "diet = false\n"
# R3 counts repeated tool targets, and the ledger is the only record that
# outlives the session: Claude installs `SessionEnd`, which deletes the spool.
LEDGER_ON = DIET_ON + "ledger = true\n"
LEDGER_OFF = DIET_OFF + "ledger = true\n"


def _record(index: int) -> dict:
    """Synthetic, and shaped like something an agent would really be handed."""
    return {
        "id": f"record-{index:04d}",
        "owner": {"name": f"user{index}", "email": f"user{index}@example.com"},
        "tags": ["alpha", "beta", "gamma"],
        "metrics": {"latency_ms": 12 + index, "bytes": 4096 + index, "ok": True},
        "note": "The quick brown fox jumps over the lazy dog while the log scrolls.",
    }


def build_json_files(workspace: Path, records: int = 50) -> list[Path]:
    """Pretty-printed and small enough to survive the client's own truncation.

    Compaction is verified: it parses both sides and keeps the result only if
    the value is unchanged. A truncated document does not parse, so the diet
    correctly does nothing to it. At 120 records the file was 46,725 bytes,
    Claude Code cut the `cat` at about 34,000, and the diet never fired in any
    session. 50 records is roughly 20,000 bytes and arrives whole.
    """
    written = []
    for number in range(1, 5):
        path = workspace / f"data-{number}.json"
        document = {
            "dataset": f"data-{number}",
            "records": [_record(number * 1000 + i) for i in range(records)],
        }
        # indent=2 is what a human-readable export looks like; the diet's whole
        # saving is the whitespace between these tokens.
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


def _settings(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _hook_settings(path: Path) -> Path:
    """The child session needs shim's hook; the diet runs nowhere else."""
    from shim_cli.clients.claude.settings import add_hook

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(add_hook(None))
    return path


def _environment(config: Path, state: Path | None = None) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("CLAUDE")
    }
    environment["SHIM_CONFIG"] = str(config)
    environment.pop("SHIM_GUARD_CONFIG", None)
    if state is not None:
        # Never the real ledger; the study writes its own and reads it back.
        # 0700 or shim refuses the directory as not private, and the failure is
        # swallowed: an empty ledger looks exactly like a session that did
        # nothing worth recording.
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        environment["SHIM_GUARD_STATE_DIR"] = str(state)
    return environment


def _command(shim: Path, client: Path, settings: Path, model: str) -> list[str]:
    return [
        str(shim),
        "watch",
        "--json",
        "--",
        str(client),
        "-p",
        TASK,
        "--settings",
        str(settings),
        "--allowedTools",
        TOOLS,
        "--permission-mode",
        "bypassPermissions",
        "--model",
        model,
        "--effort",
        "medium",
        "--no-session-persistence",
    ]


def _report_from(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "exchanges" in parsed:
            return parsed
    return None


def session(
    shim: Path,
    client: Path,
    workspace: Path,
    config: Path,
    model: str,
    label: str,
    destination: Path,
    settings: Path,
    flip_to: str = "",
    state: Path | None = None,
) -> dict:
    """One `shim watch` run wrapping one scripted client session."""
    process = subprocess.Popen(
        _command(shim, client, settings, model),
        cwd=workspace,
        env=_environment(config, state),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    flipped = False
    if flip_to:
        # The hook re-reads the settings file on every invocation, so editing it
        # mid-session is exactly the config change R1 asks about.
        deadline = time.monotonic() + FLIP_AFTER_SECONDS
        while time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.5)
        if process.poll() is None:
            config.write_text(flip_to, encoding="utf-8")
            flipped = True
    out, err = process.communicate(timeout=SESSION_TIMEOUT_SECONDS)
    stdout = out.decode("utf-8", "replace")

    document: dict = {
        "label": label,
        "exit_code": process.returncode,
        "flipped_mid_session": flipped,
    }
    report = _report_from(stdout)
    if report is not None:
        document["report"] = report
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    requests = len(report["exchanges"]) if report else 0
    print(f"{label}: exit {process.returncode}, {requests} requests")
    if report is None:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-4:]
        print("   no report parsed; stderr tail:", *tail, sep="\n     ")
    return document


def read_ledger(state: Path) -> list[dict]:
    """The decision records the session wrote: tool, target, and the sizes."""
    records = []
    for path in sorted(state.glob("**/*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(
                {
                    key: entry.get(key)
                    for key in (
                        "event",
                        "tool_name",
                        "target",
                        "action",
                        "in_bytes",
                        "out_bytes",
                        "transforms",
                    )
                }
            )
    return records


def series(document: dict) -> list[dict]:
    """The usage numbers per request, in order."""
    report = document.get("report")
    if not report:
        return []
    return [
        {
            "request_bytes": exchange["request_bytes"],
            "stop_reason": exchange["stop_reason"],
            **exchange["usage"],
        }
        for exchange in report["exchanges"]
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument(
        "--runs",
        type=int,
        default=2,
        help="repeats per configuration; Q18.1 asks for the within-configuration "
        "variance before the between-configuration difference is read",
    )
    parser.add_argument("--shim", type=Path, default=Path(shutil.which("shim") or ""))
    parser.add_argument(
        "--client", type=Path, default=Path(shutil.which("claude") or "")
    )
    parser.add_argument("--records", type=int, default=50)
    parser.add_argument("--skip-flip", action="store_true")
    parser.add_argument(
        "--ledger",
        action="store_true",
        help="one extra session per configuration with the ledger on, so R3 "
        "can count repeated tool targets and the bytes the diet removed",
    )
    arguments = parser.parse_args(argv)

    if not arguments.shim.is_file() or not arguments.client.is_file():
        parser.error("both --shim and --client must name an existing binary")

    root = arguments.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    workspace = build_workspace(root)
    build_json_files(workspace, arguments.records)
    settings = _hook_settings(root / "hook-settings.json")
    on = _settings(root / "config-diet-on.toml", DIET_ON)
    off = _settings(root / "config-diet-off.toml", DIET_OFF)

    # Q18.1: the within-configuration pair runs adjacent, so its variance is
    # measured under the same cache conditions as the difference being read.
    schedule = []
    for name, config in (("diet-on", on), ("diet-off", off)):
        schedule.extend(
            (f"{name}-{index + 1}", config) for index in range(arguments.runs)
        )

    documents = []
    for label, config in schedule:
        documents.append(
            session(
                arguments.shim,
                arguments.client,
                workspace,
                config,
                arguments.model,
                label,
                root / "reports" / f"{label}.json",
                settings,
            )
        )

    if not arguments.skip_flip:
        flip_config = _settings(root / "config-flip.toml", DIET_ON)
        documents.append(
            session(
                arguments.shim,
                arguments.client,
                workspace,
                flip_config,
                arguments.model,
                "flip-mid-session",
                root / "reports" / "flip-mid-session.json",
                settings,
                flip_to=DIET_OFF,
            )
        )

    if arguments.ledger:
        for name, body in (("ledger-on", LEDGER_ON), ("ledger-off", LEDGER_OFF)):
            state = root / "state" / name
            documents.append(
                session(
                    arguments.shim,
                    arguments.client,
                    workspace,
                    _settings(root / f"config-{name}.toml", body),
                    arguments.model,
                    name,
                    root / "reports" / f"{name}.json",
                    settings,
                    state=state,
                )
            )
            (root / f"{name}-records.json").write_text(
                json.dumps(read_ledger(state), indent=2) + "\n", encoding="utf-8"
            )

    summary = {
        document["label"]: series(document)
        for document in documents
        if "report" in document
    }
    (root / "series.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nseries written to {root / 'series.json'}")
    return 0 if all("report" in document for document in documents) else 1


if __name__ == "__main__":
    raise SystemExit(main())
