"""PRD-07: can `shim watch` sit in front of Codex at all?

Reading `openai/codex` says the design should work. Three things can only be
learned by running it: whether the shipped build behaves like `main`, whether
the WebSocket-to-HTTP fallback is clean in practice, and whether `chatgpt.com`
accepts a request re-sent by Python's `http.client` — Cloudflare fronts it and
may fingerprint the TLS client, which would sink the whole approach.

    python scripts/probe/probe_codex_watch.py --root /tmp/codexprobe

The harness stands in for `shim watch`: a loopback server that answers a
WebSocket upgrade with 426 and forwards everything else upstream the same way
`watch/proxy.py` does — `http.client.HTTPSConnection` with a default `ssl`
context — so the TLS question is asked with the client that would really ask it.

Nothing that identifies the account is recorded: header *names* only, the
presence but never the value of `ChatGPT-Account-ID`, status codes, SSE event
names, and the key names of any usage object.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
import re
import socketserver
import ssl
import subprocess
import threading
import time
from pathlib import Path

CHATGPT_HOST = "chatgpt.com"
API_HOST = "api.openai.com"
ACCOUNT_HEADER = "chatgpt-account-id"
UPSTREAM_TIMEOUT_SECONDS = 300
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
_EVENT = re.compile(rb"^event:\s*(?P<name>[\w.]+)", re.MULTILINE)
_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_.-]{8,}|Bearer\s+\S+)", re.IGNORECASE
)

RECORDS: list[dict] = []
LOCK = threading.Lock()
STARTED = time.monotonic()


def _at() -> float:
    return round(time.monotonic() - STARTED, 3)


def _scrub(text: str) -> str:
    return _SECRET.sub("<redacted>", text)


def _usage_keys(value: object, found: set[str]) -> None:
    """Key names of any `usage` object, at any depth. Never a value."""
    if isinstance(value, dict):
        usage = value.get("usage")
        if isinstance(usage, dict):
            for key, item in usage.items():
                found.add(key)
                if isinstance(item, dict):
                    for inner in item:
                        found.add(f"{key}.{inner}")
        for item in value.values():
            _usage_keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _usage_keys(item, found)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    tls_context: ssl.SSLContext = ssl.create_default_context()
    # R7b points the same harness at Anthropic to read header names only.
    forced_upstream: str = ""

    def log_message(self, *_args: object) -> None:  # keep the transcript clean
        return

    def _record(self, entry: dict) -> None:
        with LOCK:
            RECORDS.append(entry)

    def _body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if length and length.isdigit():
            return self.rfile.read(int(length))
        return b""

    def _handle(self) -> None:
        names = [name.lower() for name in self.headers.keys()]
        account = ACCOUNT_HEADER in names
        upgrade = (self.headers.get("Upgrade") or "").lower() == "websocket"
        body = self._body()

        if upgrade:
            # T2: the client tries Responses-over-WebSocket first and falls back
            # to HTTP SSE only on exactly this status.
            self._record(
                {
                    "at": _at(),
                    "kind": "upgrade",
                    "method": self.command,
                    "path": self.path,
                    "header_names": sorted(names),
                    "account_id_present": account,
                    "answered": 426,
                }
            )
            self.send_response(426)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return

        host = self.forced_upstream or (CHATGPT_HOST if account else API_HOST)
        path = self.path
        if not self.forced_upstream and not account:
            path = path.replace("/backend-api/codex/", "/v1/")

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP and key.lower() != "host"
        }
        entry = {
            "at": _at(),
            "kind": "request",
            "method": self.command,
            "path": self.path,
            "upstream_host": host,
            "upstream_path": path,
            "header_names": sorted(names),
            "account_id_present": account,
            "request_bytes": len(body),
        }

        connection = None
        try:
            connection = http.client.HTTPSConnection(
                host, timeout=UPSTREAM_TIMEOUT_SECONDS, context=self.tls_context
            )
            connection.request(self.command, path, body=body, headers=headers)
            upstream = connection.getresponse()
            payload = upstream.read()
            entry.update(
                {
                    "upstream_status": upstream.status,
                    "upstream_reason": upstream.reason,
                    "response_header_names": sorted(
                        name.lower() for name, _ in upstream.getheaders()
                    ),
                    "cf_ray_present": upstream.getheader("cf-ray") is not None,
                    "content_type": upstream.getheader("content-type", ""),
                    "response_bytes": len(payload),
                    "first_byte_at": _at(),
                }
            )
            entry.update(_describe(payload))
            self.send_response(upstream.status)
            for name, value in upstream.getheaders():
                if name.lower() in HOP_BY_HOP or name.lower() == "content-length":
                    continue
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (OSError, ValueError, http.client.HTTPException) as error:
            entry["error"] = type(error).__name__
            try:
                self.send_error(502, "probe could not forward")
            except OSError:
                pass
        finally:
            if connection is not None:
                connection.close()
            self._record(entry)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle


def _describe(payload: bytes) -> dict:
    """SSE event names in order and usage key names. Never a body."""
    events = [match.group("name").decode() for match in _EVENT.finditer(payload)]
    usage: set[str] = set()
    text = payload.decode("utf-8", "replace")
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            parsed = json.loads(line[5:].strip())
        except ValueError:
            continue
        _usage_keys(parsed, usage)
    described: dict = {"sse_events": events, "usage_keys": sorted(usage)}
    if not events:
        try:
            parsed = json.loads(text)
        except ValueError:
            described["body_head"] = _scrub(text[:400])
        else:
            _usage_keys(parsed, usage)
            described["usage_keys"] = sorted(usage)
            described["json_keys"] = sorted(parsed) if isinstance(parsed, dict) else []
            described["body_head"] = _scrub(json.dumps(parsed)[:400])
    return described


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def run_case(
    name: str,
    port: int,
    root: Path,
    use_override: bool,
    use_environment: bool,
    prompt: str,
    extra: list[str] | None = None,
) -> dict:
    base = f"http://127.0.0.1:{port}/backend-api/codex"
    command = ["codex", "exec", "--skip-git-repo-check"]
    if use_override:
        command += ["-c", f'openai_base_url="{base}"']
    command += extra or []
    command += [prompt]

    environment = dict(os.environ)
    if use_environment:
        environment["OPENAI_BASE_URL"] = base
    else:
        environment.pop("OPENAI_BASE_URL", None)

    with LOCK:
        first = len(RECORDS)
    started = _at()
    completed = subprocess.run(
        command,
        cwd=root,
        env=environment,
        capture_output=True,
        check=False,
        timeout=UPSTREAM_TIMEOUT_SECONDS,
    )
    with LOCK:
        seen = RECORDS[first:]
    output = completed.stdout.decode("utf-8", "replace")
    return {
        "case": name,
        "config_override": use_override,
        "environment_variable": use_environment,
        "started_at": started,
        "exit_code": completed.returncode,
        "reached_the_proxy": bool(seen),
        "records": seen,
        "client_said": _scrub(
            "\n".join(
                line
                for line in output.splitlines()
                if line.startswith("ERROR") or "DONE" in line
            )[:400]
        ),
    }


def run_anthropic(port: int, root: Path) -> dict:
    """R7b: one Claude Code request, for its header names only.

    PRD-16 classifies billing mode from the shape of the auth headers — an
    `x-api-key` is a key, an `authorization` bearer is a subscription — and it
    should read that off a real request rather than assume it.
    """
    environment = dict(os.environ)
    environment["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    with LOCK:
        first = len(RECORDS)
    completed = subprocess.run(
        [
            "claude",
            "-p",
            "Reply with the single word DONE",
            "--model",
            "claude-sonnet-5",
            "--effort",
            "low",
            "--no-session-persistence",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        check=False,
        timeout=UPSTREAM_TIMEOUT_SECONDS,
    )
    with LOCK:
        seen = RECORDS[first:]
    return {
        "case": "anthropic-headers",
        "exit_code": completed.returncode,
        "reached_the_proxy": bool(seen),
        "records": seen,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument(
        "--anthropic",
        action="store_true",
        help="R7b: run one Claude Code request through the harness instead",
    )
    arguments = parser.parse_args(argv)

    root = arguments.root.resolve()
    root.mkdir(parents=True, exist_ok=True)

    if arguments.anthropic:
        Handler.forced_upstream = "api.anthropic.com"

    server = Server(("127.0.0.1", arguments.port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    if arguments.anthropic:
        try:
            case = run_anthropic(arguments.port, root)
        finally:
            server.shutdown()
            server.server_close()
        (root / "anthropic-headers.json").write_text(
            json.dumps(case, indent=2) + "\n", encoding="utf-8"
        )
        for record in case["records"]:
            if record["kind"] == "request":
                print(
                    f"{record['method']} {record['path']} -> {record['upstream_host']}"
                )
                print(f"  request headers : {record['header_names']}")
                print(f"  upstream status : {record.get('upstream_status')}")
        print(f"\nwritten to {root / 'anthropic-headers.json'}")
        return 0

    try:
        version = subprocess.run(
            ["codex", "--version"], capture_output=True, check=False, timeout=30
        ).stdout.decode()
        cases = [
            run_case(
                "config-override",
                arguments.port,
                root,
                use_override=True,
                use_environment=True,
                prompt="Reply with the single word DONE",
            ),
            run_case(
                "environment-only",
                arguments.port,
                root,
                use_override=False,
                use_environment=True,
                prompt="Reply with the single word DONE",
            ),
        ]
    finally:
        server.shutdown()
        server.server_close()

    document = {
        "codex_version": version.strip(),
        "cases": cases,
    }
    (root / "codex-watch.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8"
    )
    for case in cases:
        upgrades = [r for r in case["records"] if r["kind"] == "upgrade"]
        requests = [r for r in case["records"] if r["kind"] == "request"]
        statuses = [r.get("upstream_status") for r in requests]
        print(
            f"{case['case']:<20} reached={case['reached_the_proxy']} "
            f"upgrades={len(upgrades)} requests={len(requests)} "
            f"upstream={statuses}"
        )
    print(f"\nwritten to {root / 'codex-watch.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
