"""PRD-18 R2: where does an image become visible, and what does it cost?

Three surfaces carry an image into a session, and shim sees a different amount
of each:

  - an image attached to the prompt travels in `messages[].content[]` as a
    base64 `image` block, and `UserPromptSubmit` receives the prompt text only;
  - an image read with the `Read` tool comes back in `tool_response`, which
    `PostToolUse` does see;
  - a PDF read the same way may come back as text, as images, or as both.

Each case runs once under `shim watch` with the recording hook installed, so
the same session answers both halves: which events fire and how large the hook
payload is, and how many bytes and tokens reached the wire.

    python scripts/probe/images.py --root /tmp/images

No image bytes are kept. The harness records sizes, event names and the keys
present in each payload.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe import hook_settings  # noqa: E402

SESSION_TIMEOUT_SECONDS = 600

CASES = {
    "prompt-attachment": (
        "Describe @{image} in exactly three words, then reply with only DONE.",
        "Read",
    ),
    "read-png": (
        "Use the Read tool on {image}. Then reply with only the word DONE.",
        "Read",
    ),
    "read-pdf": (
        "Use the Read tool on {pdf}. Then reply with only the word DONE.",
        "Read",
    ),
}


def _tiny_png() -> bytes:
    """A 1x1 PNG is too small to be interesting; this is 64x64 of noise."""
    import zlib

    width = height = 64
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows += bytes(((x * 7 + y * 13) % 256, (x * 3) % 256, (y * 5) % 256))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            len(body).to_bytes(4, "big")
            + kind
            + body
            + zlib.crc32(kind + body).to_bytes(4, "big")
        )

    header = (
        width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes((8, 2, 0, 0, 0))
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def _tiny_pdf() -> bytes:
    """A one-page PDF with a line of text, written by hand to avoid a library."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length 62 >>\nstream\nBT /F1 12 Tf 20 50 Td "
        b"(probe document alpha beta) Tj ET\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n"
        f"{start}\n%%EOF\n"
    ).encode()
    return bytes(out)


def build_workspace(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "shot.png").write_bytes(_tiny_png())
    (workspace / "doc.pdf").write_bytes(_tiny_pdf())
    return workspace


def _environment(captures: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("CLAUDE")
    }
    environment["SHIM_PROBE_DIR"] = str(captures)
    return environment


def describe_captures(captures: Path) -> list[dict]:
    """Event, tool, payload size and the shape of the result — never its bytes."""
    described = []
    for path in sorted(captures.glob("*.json")):
        raw = path.read_bytes()
        try:
            payload = json.loads(raw)
        except ValueError:
            described.append({"file": path.name, "bytes": len(raw), "parsed": False})
            continue
        response = payload.get("tool_response")
        described.append(
            {
                "event": payload.get("hook_event_name", ""),
                "tool": payload.get("tool_name", ""),
                "bytes": len(raw),
                "response_type": type(response).__name__,
                "response_keys": sorted(response) if isinstance(response, dict) else [],
                "base64_bytes": _base64_bytes(payload),
            }
        )
    return described


def _base64_bytes(value: object) -> int:
    """How much of this payload is an encoded blob, at any depth."""
    if isinstance(value, str):
        if len(value) > 512 and _looks_base64(value):
            return len(value)
        return 0
    if isinstance(value, dict):
        return sum(_base64_bytes(item) for item in value.values())
    if isinstance(value, list):
        return sum(_base64_bytes(item) for item in value)
    return 0


def _looks_base64(value: str) -> bool:
    sample = value[:1024]
    try:
        base64.b64decode(sample + "=" * (-len(sample) % 4), validate=True)
    except (ValueError, TypeError):
        return False
    return True


def run_case(
    shim: Path,
    client: Path,
    workspace: Path,
    root: Path,
    name: str,
    model: str,
) -> dict:
    prompt_template, tools = CASES[name]
    prompt = prompt_template.format(image="shot.png", pdf="doc.pdf")
    captures = root / "captures" / name
    captures.mkdir(parents=True, exist_ok=True)
    settings = root / f"settings-{name}.json"
    hook = [sys.executable, str(Path(__file__).resolve().parent / "capture_hook.py")]
    settings.write_text(
        json.dumps(hook_settings(hook), indent=2) + "\n", encoding="utf-8"
    )

    completed = subprocess.run(
        [
            str(shim),
            "watch",
            "--json",
            "--",
            str(client),
            "-p",
            prompt,
            "--settings",
            str(settings),
            "--allowedTools",
            tools,
            "--permission-mode",
            "bypassPermissions",
            "--model",
            model,
            "--effort",
            "low",
            "--no-session-persistence",
        ],
        cwd=workspace,
        env=_environment(captures),
        capture_output=True,
        check=False,
        timeout=SESSION_TIMEOUT_SECONDS,
    )
    report = None
    for line in reversed(completed.stdout.decode("utf-8", "replace").splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "exchanges" in parsed:
            report = parsed
            break

    result = {
        "case": name,
        "prompt": prompt,
        "exit_code": completed.returncode,
        "hook_events": describe_captures(captures),
        "wire": {
            "requests": report["requests"] if report else 0,
            "sections": report["approximate"]["tokens_by_section"] if report else {},
            "request_bytes": [e["request_bytes"] for e in report["exchanges"]]
            if report
            else [],
            "usage": [e["usage"] for e in report["exchanges"]] if report else [],
        },
    }
    events = ", ".join(
        sorted({item.get("event", "?") for item in result["hook_events"]})
    )
    print(f"{name}: exit {completed.returncode}, events [{events}]")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--shim", type=Path, default=Path(shutil.which("shim") or ""))
    parser.add_argument(
        "--client", type=Path, default=Path(shutil.which("claude") or "")
    )
    parser.add_argument("--only", nargs="*", default=())
    arguments = parser.parse_args(argv)

    if not arguments.shim.is_file() or not arguments.client.is_file():
        parser.error("both --shim and --client must name an existing binary")

    root = arguments.root.resolve()
    workspace = build_workspace(root)
    names = arguments.only or tuple(CASES)
    results = [
        run_case(
            arguments.shim, arguments.client, workspace, root, name, arguments.model
        )
        for name in names
    ]
    (root / "images.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nwritten to {root / 'images.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
