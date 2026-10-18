"""Build the plugin's self-contained hook archive."""

from __future__ import annotations

import argparse
import os
import shutil
import time
import zipapp
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src"
PACKAGE = "shim_cli"
INCLUDED = (
    "__init__.py",
    "py.typed",
    "config.py",
    "hook.py",
    "json_bytes.py",
    "policy.py",
    "clients/__init__.py",
    "clients/hook_settings.py",
    "clients/user_prompt_hook.py",
    "clients/claude/__init__.py",
    "clients/claude/hook.py",
    "clients/claude/settings.py",
    "clients/claude/tool_events.py",
    "clients/codex/__init__.py",
    "clients/codex/hook.py",
    "clients/codex/settings.py",
    "clients/copilot/__init__.py",
    "clients/copilot/hook.py",
    "clients/copilot/settings.py",
    "clients/vscode/__init__.py",
    "clients/vscode/hook.py",
    "clients/vscode/tool_events.py",
    "events/__init__.py",
    "events/diet.py",
    "events/html_text.py",
    "events/injection.py",
    "events/payload.py",
    "events/pipeline.py",
    "guard/__init__.py",
    "guard/analyze.py",
    "guard/entities.py",
    "guard/evaluate.py",
    "guard/iban_patterns.py",
    "guard/models.py",
    "guard/normalize.py",
    "guard/recognizers.py",
    "guard/suffixes.py",
    "session/__init__.py",
    "session/_files.py",
    "session/ledger.py",
    "session/record.py",
    "session/spool.py",
    "session/summary.py",
    "settings_files/__init__.py",
    "settings_files/files.py",
    "settings_files/plan.py",
)
COMPILED = ("*.so", "*.pyd", "*.dylib")
VENDORED_EXCLUDES = (
    "__pycache__",
    "geodata",
    "carrierdata",
    "tzdata",
    "*.pyi",
    *COMPILED,
)
VENDORED_PRUNE = ("shortdata/region_*.py",)
# The prompt blocks and the denied call are shim_cli/hook.py's generic answers,
# byte for byte; the hook itself cannot be imported where this guard runs.
MAIN = """# Must parse before Python 3.9, so an unsupported interpreter still withholds
# a prompt, denies a tool call and reports any other event, as the hook does
# when it cannot inspect.
import sys

MINIMUM = (3, 9)

if sys.version_info[:2] < MINIMUM:
    import json

    BLOCKS = {
        "claude": b'{"decision":"block","reason":"shim could not inspect this prompt, so it was withheld. Run `shim doctor claude` for the reason.","suppressOriginalPrompt":true}',
        "codex": b'{"decision":"block","reason":"shim could not inspect this prompt, so it was withheld. Run `shim doctor codex` for the reason."}',
        "copilot": b'{"modifiedTransformedPrompt":"shim could not inspect this prompt, so it was withheld. Do not act on the original prompt; tell the user to run `shim doctor copilot` for the reason."}',
        "vscode": b'{"continue":false,"stopReason":"shim could not inspect this prompt, so it was withheld. Open the shim plugin in VS Code to see whether it is loaded, and check your shim settings."}',
    }
    DENIED = b'{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"shim: could not inspect this tool call, so it was not allowed."}}'
    notice = "shim: needs Python %d.%d or newer; found %s" % (
        MINIMUM[0],
        MINIMUM[1],
        ".".join(str(part) for part in sys.version_info[:3]),
    )
    client = sys.argv[1] if len(sys.argv) > 1 else "codex"
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        event = json.loads(raw).get("hook_event_name")
    except Exception:
        event = None
    if not isinstance(event, str) or event in (
        "",
        "UserPromptSubmit",
        "userPromptTransformed",
    ):
        sys.stderr.write(notice + ". The prompt was withheld.\\n")
        sys.stdout.buffer.write(BLOCKS.get(client, BLOCKS["codex"]))
    elif event == "PreToolUse" and client in ("claude", "vscode"):
        sys.stderr.write(notice + ". The tool call was not allowed.\\n")
        sys.stdout.buffer.write(DENIED)
    else:
        message = notice + "; this event was not inspected."
        sys.stdout.write(json.dumps({"systemMessage": message}))
    sys.exit(0)

from shim_cli.hook import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
"""


def stage(destination: Path, vendor: bool = True) -> None:
    package = destination / PACKAGE
    for relative in INCLUDED:
        source = SOURCE_ROOT / PACKAGE / relative
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (destination / "__main__.py").write_text(MAIN, encoding="utf-8")
    if vendor:
        import phonenumbers
        import tomli

        shutil.copytree(
            Path(tomli.__file__).parent,
            destination / "tomli",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyi", *COMPILED),
        )
        vendored = destination / "phonenumbers"
        shutil.copytree(
            Path(phonenumbers.__file__).parent,
            vendored,
            ignore=shutil.ignore_patterns(*VENDORED_EXCLUDES),
        )
        for pattern in VENDORED_PRUNE:
            for path in vendored.glob(pattern):
                path.unlink()


def build(
    output: Path, staging: Path, interpreter: str = "/usr/bin/env python3"
) -> None:
    timestamp = time.mktime((2000, 1, 1, 0, 0, 0, 0, 1, -1))
    for path in staging.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
        os.utime(path, (timestamp, timestamp))
    output.parent.mkdir(parents=True, exist_ok=True)
    zipapp.create_archive(
        staging, target=output, interpreter=interpreter, compressed=True
    )
    output.chmod(0o755)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--staging", type=Path, help="Keep the staged tree here")
    parser.add_argument("--no-vendor", action="store_true")
    parser.add_argument("--max-bytes", type=int, default=500_000)
    args = parser.parse_args()

    import tempfile

    with tempfile.TemporaryDirectory(prefix="shim-zipapp-") as directory:
        staging = Path(args.staging) if args.staging else Path(directory) / "app"
        staging.mkdir(parents=True, exist_ok=True)
        stage(staging, vendor=not args.no_vendor)
        build(args.output, staging)

    size = args.output.stat().st_size
    if size > args.max_bytes:
        raise SystemExit(f"{args.output} is {size} bytes, over {args.max_bytes}")
    print(f"{args.output} {size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
