from __future__ import annotations

import gzip
import http.client
import http.server
import io
import json
import pathlib
import socket
import socketserver
import sys
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest

from shim_cli.watch import proxy

MESSAGE_START = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"model":"claude-sonnet-5",'
    b'"usage":{"input_tokens":2,"cache_creation_input_tokens":18093,'
    b'"cache_read_input_tokens":91562,"output_tokens":5}}}\n\n'
)
MESSAGE_DELTA = (
    b'event: message_delta\ndata: {"type":"message_delta",'
    b'"usage":{"output_tokens":214}}\n\n'
)


class _Upstream:
    def __init__(self, *, gzip_body: bool = True, delay: float = 0.0) -> None:
        self.seen: list = []
        self.gzip_body = gzip_body
        self.delay = delay
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.seen.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                parts = [MESSAGE_START, MESSAGE_DELTA]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                if outer.gzip_body:
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("x-provider-header", "kept")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for part in parts:
                    payload = gzip.compress(part) if outer.gzip_body else part
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(payload), payload))
                    self.wfile.flush()
                    if outer.delay:
                        time.sleep(outer.delay)
                self.wfile.write(b"0\r\n\r\n")

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream():
    server = _Upstream()
    yield server
    server.stop()


@pytest.fixture
def watched(monkeypatch, upstream):
    port = upstream.port

    class Plain(http.client.HTTPConnection):
        def __init__(self, host, timeout=None, context=None):
            super().__init__("127.0.0.1", port, timeout=timeout or 30)

    monkeypatch.setattr(http.client, "HTTPSConnection", Plain)
    running = proxy.start("api.anthropic.com")
    yield running, upstream
    running.stop()


def _post(running, body: bytes, headers: dict, path: str = "/v1/messages?beta=true"):
    request = urllib.request.Request(
        running.base_url + path, data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = response.status, dict(response.headers), response.read()
    running.session.drain(10)
    return result


BODY = json.dumps(
    {
        "model": "claude-sonnet-5",
        "system": "be brief",
        "tools": [{"name": "Read"}],
        "messages": [{"role": "user", "content": "hello"}],
    }
).encode()

HEADERS = {
    "Content-Type": "application/json",
    "anthropic-beta": "oauth-2025-04-20",
    "anthropic-version": "2023-06-01",
    "authorization": "Bearer sk-ant-oat-fake",
    "x-app": "cli",
}


def test_the_request_reaches_the_provider_byte_identical(watched) -> None:
    running, upstream = watched

    status, _headers, _body = _post(running, BODY, HEADERS)

    assert status == 200
    assert upstream.seen[0]["body"] == BODY


def test_the_headers_that_carry_the_sign_in_are_forwarded_verbatim(watched) -> None:
    running, upstream = watched

    _post(running, BODY, HEADERS)

    seen = upstream.seen[0]["headers"]
    assert seen["anthropic-beta"] == "oauth-2025-04-20"
    assert seen["anthropic-version"] == "2023-06-01"
    assert seen["authorization"] == "Bearer sk-ant-oat-fake"
    assert seen["x-app"] == "cli"


def test_the_query_string_survives(watched) -> None:
    running, upstream = watched

    _post(running, BODY, HEADERS)

    assert upstream.seen[0]["path"] == "/v1/messages?beta=true"


def test_hop_by_hop_headers_are_not_relayed(watched) -> None:
    running, upstream = watched

    _post(running, BODY, dict(HEADERS, Connection="keep-alive", TE="trailers"))

    seen = upstream.seen[0]["headers"]
    assert "te" not in seen


def test_the_provider_s_own_response_headers_are_kept(watched) -> None:
    running, _upstream = watched

    _status, headers, _body = _post(running, BODY, HEADERS)

    assert headers.get("x-provider-header") == "kept"


def test_response_headers_with_line_breaks_are_dropped() -> None:
    class Response:
        status = 200

        def getheaders(self):
            return (
                ("x-provider-header", "kept"),
                ("x-bad\nInjected", "name"),
                ("x-bad", "value\r\nInjected: yes"),
            )

        def getheader(self, _name):
            return None

        def read1(self, _size):
            return b""

    handler = object.__new__(proxy._Handler)
    sent = []
    handler.session = proxy.Session()
    handler.wfile = io.BytesIO()
    handler.send_response = lambda _status: None
    handler.send_header = lambda name, value: sent.append((name, value))
    handler.end_headers = lambda: None

    handler._stream(Response(), proxy.Exchange())

    assert sent == [
        ("x-provider-header", "kept"),
        ("Connection", "close"),
        ("Transfer-Encoding", "chunked"),
    ]


def test_the_client_receives_the_bytes_the_provider_sent(watched) -> None:
    running, _upstream = watched

    _status, headers, body = _post(running, BODY, HEADERS)

    assert headers.get("Content-Encoding") == "gzip"
    decoded = gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
    assert b"message_start" in decoded


def test_usage_is_read_off_a_gzipped_stream(watched) -> None:
    running, _upstream = watched

    _post(running, BODY, HEADERS)

    exchange = running.session.exchanges[0]
    assert exchange.usage.total_input == 2 + 18093 + 91562
    assert exchange.usage.output_tokens == 214
    assert exchange.status == 200


def test_the_request_is_measured_without_being_kept(watched) -> None:
    running, _upstream = watched

    _post(running, BODY, HEADERS)

    exchange = running.session.exchanges[0]
    assert exchange.sections["tools"] > 0
    assert exchange.model == "claude-sonnet-5"
    assert exchange.path == "/v1/messages"
    assert "hello" not in json.dumps(exchange.sections)


def test_an_unreachable_provider_is_reported_not_invented(monkeypatch) -> None:
    class Refused(http.client.HTTPConnection):
        def __init__(self, host, timeout=None, context=None):
            super().__init__("127.0.0.1", 1, timeout=1)

    monkeypatch.setattr(http.client, "HTTPSConnection", Refused)
    running = proxy.start("api.anthropic.com")
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            _post(running, BODY, HEADERS)
        assert caught.value.code == 502
        assert running.session.errors == 1
        assert running.session.exchanges == []
    finally:
        running.stop()


def test_the_proxy_binds_to_loopback_only() -> None:
    running = proxy.start("api.anthropic.com")
    try:
        assert running._server.server_address[0] == "127.0.0.1"
        assert running.base_url.startswith("http://127.0.0.1:")
    finally:
        running.stop()


def test_the_stream_is_relayed_in_pieces_rather_than_at_the_end(
    monkeypatch,
) -> None:
    upstream = _Upstream(delay=0.35)
    port = upstream.port

    class Plain(http.client.HTTPConnection):
        def __init__(self, host, timeout=None, context=None):
            super().__init__("127.0.0.1", port, timeout=30)

    monkeypatch.setattr(http.client, "HTTPSConnection", Plain)
    running = proxy.start("api.anthropic.com")
    try:
        request = urllib.request.Request(
            running.base_url + "/v1/messages", data=BODY, headers=HEADERS
        )
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=30) as response:
            first = response.read(1)
            first_at = time.monotonic() - started
            response.read()
        total = time.monotonic() - started
    finally:
        running.stop()
        upstream.stop()

    assert first
    assert first_at < total - 0.2, f"first byte at {first_at:.2f}s of {total:.2f}s"


def test_a_failed_upstream_read_is_not_framed_as_a_complete_response(
    monkeypatch,
) -> None:
    class Response:
        status = 200
        reads = 0

        def getheaders(self):
            return ()

        def getheader(self, _name):
            return None

        def read1(self, _size):
            self.reads += 1
            if self.reads == 1:
                return b"partial"
            raise OSError("upstream stream failed")

    class Connection:
        def __init__(self, *_args, **_kwargs):
            self.response = Response()

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return self.response

        def close(self):
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    running = proxy.start("api.anthropic.com")
    try:
        request = (
            b"POST /v1/messages HTTP/1.1\r\n"
            b"Host: api.anthropic.com\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(BODY)}\r\n\r\n".encode()
            + BODY
        )
        with socket.create_connection(("127.0.0.1", running.port), timeout=5) as client:
            client.sendall(request)
            response = bytearray()
            while chunk := client.recv(4096):
                response.extend(chunk)
        running.session.drain(5)
    finally:
        running.stop()

    _headers, body = bytes(response).split(b"\r\n\r\n", 1)
    assert body == b"7\r\npartial\r\n"
    assert running.session.errors == 1
    assert running.session.exchanges[0].status == 200


def test_measurement_failing_never_fails_the_request(watched, monkeypatch) -> None:
    running, upstream = watched

    def explode(*_args, **_kwargs):
        raise RuntimeError("measurement is broken")

    monkeypatch.setattr(proxy, "inspect_request", explode)

    status, _headers, _body = _post(running, BODY, HEADERS)

    assert status == 200
    assert upstream.seen[0]["body"] == BODY


def test_an_uncompressed_stream_is_read_too(monkeypatch) -> None:
    upstream = _Upstream(gzip_body=False)
    port = upstream.port

    class Plain(http.client.HTTPConnection):
        def __init__(self, host, timeout=None, context=None):
            super().__init__("127.0.0.1", port, timeout=30)

    monkeypatch.setattr(http.client, "HTTPSConnection", Plain)
    running = proxy.start("api.anthropic.com")
    try:
        _post(running, BODY, HEADERS)
        assert running.session.exchanges[0].usage.output_tokens == 214
    finally:
        running.stop()
        upstream.stop()


def test_no_request_body_is_written_anywhere_on_disk(watched, tmp_path) -> None:
    running, _upstream = watched
    marker = "ZQX-CANARY-9f13ab-DO-NOT-PERSIST"
    body = json.dumps(
        {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": marker}],
        }
    ).encode()

    _post(running, body, HEADERS)

    written = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and marker.encode() in path.read_bytes()
    ]
    assert written == []
    kept = json.dumps(
        [
            {
                "sections": exchange.sections,
                "entities": exchange.entities,
                "path": exchange.path,
                "model": exchange.model,
            }
            for exchange in running.session.exchanges
        ]
    )
    assert marker not in kept


def test_concurrent_requests_are_all_recorded(watched) -> None:
    running, _upstream = watched
    failures: list = []

    def attempt() -> None:
        try:
            _post(running, BODY, HEADERS)
        except Exception as error:
            failures.append(repr(error))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == []
    assert [thread.is_alive() for thread in threads] == [False] * 8
    running.stop()

    assert len(running.session.exchanges) == 8


def test_a_request_still_in_flight_is_counted_before_the_report(
    monkeypatch, upstream
) -> None:
    port = upstream.port

    class Slow(http.client.HTTPConnection):
        def __init__(self, host, timeout=None, context=None):
            super().__init__("127.0.0.1", port, timeout=30)

    monkeypatch.setattr(http.client, "HTTPSConnection", Slow)
    running = proxy.start("api.anthropic.com")
    original = proxy._Handler._forward

    def lingering(self) -> None:
        original(self)
        time.sleep(0.6)

    monkeypatch.setattr(proxy._Handler, "_forward", lingering)
    thread = threading.Thread(target=_post, args=(running, BODY, HEADERS))
    thread.start()
    thread.join(timeout=30)

    running.stop()

    assert len(running.session.exchanges) == 1


def test_stopping_twice_is_harmless() -> None:
    running = proxy.start("api.anthropic.com")
    running.stop()
    running.stop()


def test_measurement_runs_on_a_worker_thread_without_the_signal_deadline(
    watched,
) -> None:
    from shim_cli.guard import evaluate

    running, _upstream = watched
    running._server.RequestHandlerClass.evaluate = staticmethod(evaluate)
    body = json.dumps(
        {
            "model": "claude-sonnet-5",
            "tools": [{"name": "Read"}],
            "system": "be brief",
            "messages": [
                {"role": "user", "content": "mail ops@example.com about the deploy"}
            ],
        }
    ).encode()

    _post(running, body, HEADERS)

    exchange = running.session.exchanges[0]
    assert exchange.measured is True
    assert exchange.sections["tools"] > 0
    assert exchange.entities == {"EMAIL": 1}


def test_the_request_is_on_its_way_before_it_is_measured(watched, monkeypatch) -> None:
    running, upstream = watched
    order: list = []

    original = proxy._Handler._measure

    def noted(self, body, exchange):
        order.append("measured")
        return original(self, body, exchange)

    monkeypatch.setattr(proxy._Handler, "_measure", noted)
    real_request = http.client.HTTPConnection.request

    def watched_request(self, *args, **kwargs):
        order.append("sent")
        return real_request(self, *args, **kwargs)

    monkeypatch.setattr(http.client.HTTPConnection, "request", watched_request)

    _post(running, BODY, HEADERS)

    assert "measured" in order
    assert order[order.index("measured") - 1] == "sent"
    assert upstream.seen


@pytest.mark.parametrize(
    "framing",
    [
        b"Transfer-Encoding: chunked",
        b"Content-Length: -1",
        b"Content-Length: nope",
        b"Content-Length: 1\r\nContent-Length: 1",
    ],
)
def test_invalid_framing_never_contacts_provider(watched, framing):
    running, upstream = watched
    with socket.create_connection(("127.0.0.1", running.port), timeout=3) as client:
        client.sendall(
            b"POST /v1/messages HTTP/1.1\r\nHost: local\r\n" + framing + b"\r\n\r\n"
        )
        assert b"400" in client.recv(4096)
    assert not upstream.seen


def test_request_beyond_measurement_limit_is_forwarded_whole(watched, monkeypatch):
    running, upstream = watched
    monkeypatch.setattr(proxy, "MAX_BODY_BYTES", 100)
    status, _, _ = _post(running, BODY * 2000, HEADERS)
    assert status == 200
    assert upstream.seen[0]["body"] == BODY * 2000
    assert not running.session.exchanges[0].measured


def test_slow_measurement_does_not_delay_response_or_shutdown(watched, monkeypatch):
    running, _ = watched
    entered = threading.Event()
    release = threading.Event()

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return proxy.Exchange()

    monkeypatch.setattr(proxy, "inspect_request", slow)
    monkeypatch.setattr(proxy.Watch, "DRAIN_SECONDS", 0.05)
    try:
        with urllib.request.urlopen(
            urllib.request.Request(running.base_url + "/v1/messages", data=BODY),
            timeout=2,
        ) as response:
            assert response.read()
        assert entered.wait(1)
        started = time.monotonic()
        running.stop()
        assert time.monotonic() - started < 1.5
        assert not running.session.exchanges[0].measured
        from shim_cli.watch import report

        assert report.as_json(running.session, 1)["inspection_incomplete"] == 1
    finally:
        release.set()
        running.session.drain(2)


@pytest.mark.parametrize("failure", ["request", "getresponse"])
def test_upstream_connection_closes_on_failure(monkeypatch, failure):
    from unittest.mock import Mock

    connection = Mock()
    getattr(connection, failure).side_effect = OSError("synthetic failure")
    monkeypatch.setattr(
        http.client, "HTTPSConnection", lambda *args, **kwargs: connection
    )
    running = proxy.start()
    try:
        with pytest.raises(urllib.error.HTTPError):
            _post(running, BODY, HEADERS)
        assert running.session.drain(2)
        connection.close.assert_called_once()
    finally:
        running.stop()


@pytest.mark.parametrize("truncated", [True, False], ids=["truncated", "slow"])
def test_incomplete_request_body_has_bounded_failure(monkeypatch, truncated):
    from unittest.mock import Mock

    connection = Mock()
    connection.request.side_effect = lambda *args, **kwargs: list(kwargs["body"])
    monkeypatch.setattr(
        http.client, "HTTPSConnection", lambda *args, **kwargs: connection
    )
    monkeypatch.setattr(proxy, "DOWNSTREAM_TIMEOUT_SECONDS", 0.15)
    running = proxy.start()
    try:
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", running.port), timeout=2) as client:
            client.sendall(
                b"POST /v1/messages HTTP/1.1\r\nHost: local\r\nContent-Length: 10\r\n\r\nx"
            )
            if truncated:
                client.shutdown(socket.SHUT_WR)
            assert b"502" in client.recv(4096)
        assert running.session.drain(1)
        assert time.monotonic() - started < 1.5
        connection.close.assert_called_once()
        connection.getresponse.assert_not_called()
    finally:
        running.stop()


def test_saturated_measurement_still_forwards(watched, monkeypatch):
    from unittest.mock import Mock

    running, upstream = watched
    slots = running.session._measurement_slots
    assert slots.acquire(False) and slots.acquire(False)
    inspect = Mock()
    monkeypatch.setattr(proxy, "inspect_request", inspect)
    try:
        assert _post(running, BODY, HEADERS)[0] == 200
        assert upstream.seen[0]["body"] == BODY
        assert not running.session.exchanges[0].measured
        inspect.assert_not_called()
    finally:
        slots.release()
        slots.release()


def test_decompression_budget_never_changes_forwarded_bytes(monkeypatch):
    compressed = gzip.compress(b"x" * 100_000)

    class Response:
        status = 200
        chunks = iter([compressed, b""])

        def getheaders(self):
            return [("Content-Encoding", "gzip")]

        def getheader(self, name):
            return "gzip" if name == "Content-Encoding" else "text/event-stream"

        def read1(self, size):
            return next(self.chunks)

    handler = object.__new__(proxy._Handler)
    handler.session = proxy.Session()
    handler.wfile = io.BytesIO()
    handler.send_response = lambda status: None
    handler.send_header = lambda name, value: None
    handler.end_headers = lambda: None
    monkeypatch.setattr(proxy, "MAX_BODY_BYTES", 100)
    exchange = proxy.Exchange()
    handler._stream(Response(), exchange)
    assert handler.wfile.getvalue() == b"%x\r\n%s\r\n0\r\n\r\n" % (
        len(compressed),
        compressed,
    )
    assert exchange.usage_status == "unavailable"


IBAN = "TR330006100519786457841326"


@pytest.fixture
def scanned(monkeypatch, upstream):
    """A watch whose detector is supplied by the test."""
    port = upstream.port

    class Plain(http.client.HTTPConnection):
        def __init__(self, host, timeout=None, context=None):
            super().__init__("127.0.0.1", port, timeout=timeout or 30)

    monkeypatch.setattr(http.client, "HTTPSConnection", Plain)
    started: list = []

    def start(evaluate):
        running = proxy.start("api.anthropic.com", evaluate)
        started.append(running)
        return running

    yield start
    for running in started:
        running.stop()


def _strings(value, seen=None, depth: int = 0):
    seen = set() if seen is None else seen
    if depth > 12 or id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key, seen, depth + 1)
            yield from _strings(item, seen, depth + 1)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _strings(item, seen, depth + 1)
        return
    inner = getattr(value, "__dict__", None)
    if inner:
        yield from _strings(inner, seen, depth + 1)


def test_the_whole_request_is_scanned_and_none_of_it_is_kept(scanned) -> None:
    from shim_cli.guard import evaluate

    running = scanned(evaluate)
    body = json.dumps(
        {
            "model": "claude-sonnet-5",
            "system": f"policy {IBAN}",
            "tools": [{"name": "Read", "description": f"example {IBAN}"}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": f"file said {IBAN}",
                        }
                    ],
                }
            ],
        }
    ).encode()

    _post(running, body, HEADERS)

    exchange = running.session.exchanges[0]
    assert exchange.entities["IBAN"] == 3
    assert exchange.entities_by_section["messages"]["IBAN"] == 1
    for text in _strings(running.session):
        assert IBAN not in text
        assert "policy" not in text


def test_a_large_tool_set_is_scanned_once_across_a_session(scanned) -> None:
    from types import SimpleNamespace

    lengths: list[int] = []

    def counting(text: str):
        lengths.append(len(text))
        return SimpleNamespace(counts=())

    running = scanned(counting)
    body = json.dumps(
        {
            "model": "claude-sonnet-5",
            "tools": [{"name": "Read", "description": "d" * 150_000}],
            "messages": [{"role": "user", "content": "hello"}],
        }
    ).encode()

    for _ in range(3):
        _post(running, body, HEADERS)

    assert len(running.session.exchanges) == 3
    assert sum(1 for length in lengths if length > 100_000) == 1
    assert lengths.count(5) == 3


def test_a_truncated_response_is_recorded_and_relayed_unchanged(
    watched, monkeypatch
) -> None:
    import sys

    running, _upstream = watched
    delta = (
        b"event: message_delta\n"
        b'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},'
        b'"usage":{"output_tokens":214}}\n\n'
    )
    monkeypatch.setattr(sys.modules[__name__], "MESSAGE_DELTA", delta)

    _status, _headers, body = _post(running, BODY, HEADERS)

    assert running.session.exchanges[0].stop_reason == "max_tokens"
    decoded = gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
    assert delta in decoded


FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "watch"


class _Streaming(_Upstream):
    """An upstream that replays a captured SSE fixture instead of the default one."""

    def __init__(self, name: str, **changes) -> None:
        self.parts = (FIXTURES / name).read_bytes().split(b"\n\n")
        super().__init__(**changes)


def _replaying(monkeypatch, name: str, evaluate=None, **changes):
    upstream = _Streaming(name, **changes)
    port = upstream.port

    class Plain(http.client.HTTPConnection):
        def __init__(self, host, timeout=None, context=None):
            super().__init__("127.0.0.1", port, timeout=timeout or 30)

    monkeypatch.setattr(http.client, "HTTPSConnection", Plain)
    monkeypatch.setattr(
        sys.modules[__name__], "MESSAGE_START", upstream.parts[0] + b"\n\n"
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "MESSAGE_DELTA",
        b"\n\n".join(upstream.parts[1:]),
    )
    running = proxy.start("api.anthropic.com", evaluate)
    return running, upstream


def test_a_value_split_across_two_chunks_is_still_found(monkeypatch) -> None:
    from shim_cli.guard import evaluate

    running, upstream = _replaying(monkeypatch, "split-iban.sse", evaluate, delay=0.15)
    try:
        _post(running, BODY, HEADERS)
    finally:
        running.stop()
        upstream.stop()

    exchange = running.session.exchanges[0]
    assert exchange.response_entities == {"text": {"IBAN": 1}}
    assert exchange.response_scan_status == "known"


def test_the_client_never_waits_for_the_detector(monkeypatch) -> None:
    """The wire order is unobservable from outside; being unblocked is the point."""
    entered = threading.Event()
    release = threading.Event()

    def blocking(text: str):
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(counts=())

    running, upstream = _replaying(monkeypatch, "split-iban.sse", blocking)
    try:
        request = urllib.request.Request(
            running.base_url + "/v1/messages", data=BODY, headers=HEADERS
        )
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.read()
        elapsed = time.monotonic() - started

        assert entered.wait(2), "the detector never ran"
        assert elapsed < 1.0, f"the client waited {elapsed:.2f}s"
    finally:
        release.set()
        running.session.drain(5)
        running.stop()
        upstream.stop()


def test_without_a_free_slot_the_response_is_not_scanned(monkeypatch) -> None:
    calls: list[str] = []

    def counting(text: str):
        calls.append(text)
        return SimpleNamespace(counts=())

    running, upstream = _replaying(monkeypatch, "split-iban.sse", counting)
    slots = running.session._measurement_slots
    assert slots.acquire(False) and slots.acquire(False)
    try:
        assert _post(running, BODY, HEADERS)[0] == 200
    finally:
        slots.release()
        slots.release()
        running.stop()
        upstream.stop()

    exchange = running.session.exchanges[0]
    assert exchange.response_scan_status == "unavailable"
    assert exchange.response_entities == {}
    assert calls == []


def test_no_response_text_survives_the_exchange(monkeypatch, tmp_path) -> None:
    from shim_cli.guard import evaluate

    running, upstream = _replaying(monkeypatch, "thinking.sse", evaluate)
    try:
        _post(running, BODY, HEADERS)
    finally:
        running.stop()
        upstream.stop()

    assert running.session.exchanges[0].response_entities == {"thinking": {"IBAN": 1}}
    for text in _strings(running.session):
        assert IBAN not in text
        assert "removed the duplicate" not in text
    written = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and IBAN.encode() in path.read_bytes()
    ]
    assert written == []


def test_scanning_changes_none_of_the_bytes_the_client_receives(monkeypatch) -> None:
    from shim_cli.guard import evaluate

    seen = []
    for detector in (None, evaluate):
        running, upstream = _replaying(monkeypatch, "thinking.sse", detector)
        try:
            seen.append(_post(running, BODY, HEADERS))
        finally:
            running.stop()
            upstream.stop()

    (status, headers, body), (other_status, other_headers, other_body) = seen
    # gzip stamps the time it ran, so compare what the client actually decodes.
    assert status == other_status
    assert gzip.decompress(body) == gzip.decompress(other_body)
    assert headers.get("Content-Encoding") == other_headers.get("Content-Encoding")
