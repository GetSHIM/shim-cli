"""The proxy relays what was sent and what came back, and says when it could
not measure. A fake provider on loopback; nothing leaves the machine."""

from __future__ import annotations

import http.client
import http.server
import json
import socket
import socketserver
import sys
import threading
import urllib.request

import pytest

from shim_cli.guard import evaluate
from shim_cli.watch import measure, proxy, report

BODY = json.dumps(
    {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "mail jane.doe@example.com"}],
    }
).encode()
STREAM = (
    b'data: {"type":"content_block_delta","index":0,"delta":'
    b'{"type":"text_delta","text":"hello"}}\n\n'
    b'data: {"type":"message_delta","usage":{"input_tokens":1,"output_tokens":2}}\n\n'
)


class _Provider:
    """Answers every POST with `chunks`, and records the headers it received."""

    def __init__(self, chunks=(STREAM,), content_type="text/event-stream") -> None:
        self.received: list = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.received.append(self.headers)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.write(b"0\r\n\r\n")

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def provider(monkeypatch):
    made: list = []

    def make(**options):
        server = _Provider(**options)
        made.append(server)
        port = server.server.server_address[1]

        class Plain(http.client.HTTPConnection):
            def __init__(self, host, timeout=None, context=None):
                super().__init__("127.0.0.1", port, timeout=timeout or 30)

        monkeypatch.setattr(http.client, "HTTPSConnection", Plain)
        return server

    yield make
    for server in made:
        server.stop()


def _send(running, headers=()):
    connection = http.client.HTTPConnection("127.0.0.1", running.port, timeout=30)
    connection.putrequest("POST", "/v1/messages")
    for name, value in (("Content-Length", str(len(BODY))), *headers):
        connection.putheader(name, value)
    connection.endheaders(BODY)
    response = connection.getresponse()
    result = response, response.read()
    connection.close()
    running.session.drain(10)
    return result


def test_repeated_request_headers_are_forwarded_as_sent(provider) -> None:
    server = provider()
    running = proxy.start()
    try:
        _send(
            running,
            [
                ("anthropic-beta", "interleaved-thinking-2025-05-14"),
                ("anthropic-beta", "oauth-2025-04-20"),
            ],
        )
    finally:
        running.stop()

    assert server.received[0].get_all("anthropic-beta") == [
        "interleaved-thinking-2025-05-14",
        "oauth-2025-04-20",
    ]


def test_the_client_receives_the_provider_s_server_and_date_only(provider) -> None:
    provider()
    running = proxy.start()
    try:
        response, _body = _send(running)
    finally:
        running.stop()

    assert len(response.msg.get_all("Server")) == 1
    assert len(response.msg.get_all("Date")) == 1


@pytest.mark.parametrize(
    "event",
    [
        {"type": "content_block_delta", "index": 0, "delta": {"type": ["x"]}},
        {
            "type": "content_block_delta",
            "index": [0],
            "delta": {"type": "text_delta", "text": "x"},
        },
    ],
)
def test_a_stream_of_an_unexpected_shape_is_relayed_whole(provider, event) -> None:
    stream = f"data: {json.dumps(event)}\n\n".encode() + STREAM
    provider(chunks=[stream])
    running = proxy.start()
    try:
        _response, body = _send(running)
    finally:
        running.stop()

    assert body == stream


@pytest.mark.parametrize("where", ["feed", "finish"])
@pytest.mark.parametrize(
    "content_type,chunks",
    [("text/event-stream", [STREAM]), ("application/json", [b'{"usage":{}}'])],
)
def test_a_reader_that_raises_never_cuts_the_response(
    provider, monkeypatch, where, content_type, chunks
) -> None:
    def explode(*_args):
        raise RuntimeError("synthetic reader failure")

    monkeypatch.setattr(measure.UsageReader, where, explode)
    provider(chunks=chunks, content_type=content_type)
    running = proxy.start()
    try:
        _response, body = _send(running)  # IncompleteRead if unterminated
    finally:
        running.stop()

    assert body == b"".join(chunks)
    (exchange,) = running.session.exchanges
    assert exchange.usage_status == "unavailable"


def test_a_request_still_being_measured_at_shutdown_says_so(
    provider, monkeypatch
) -> None:
    provider()
    release = threading.Event()
    entered = threading.Event()

    def slow(text):
        entered.set()
        release.wait(10)
        return evaluate(text)

    monkeypatch.setattr(proxy.Watch, "DRAIN_SECONDS", 0.2)
    running = proxy.start("api.anthropic.com", slow)
    try:
        _send_without_waiting(running)
        assert entered.wait(10)
        running.stop()
        (exchange,) = running.session.exchanges
        measured, reason = exchange.measured, exchange.incomplete_reason
        document = report.as_json(running.session, 1)
        text = report.render(running.session, 1)
    finally:
        release.set()
        running.session.drain(10)

    assert not measured
    assert reason == measure.UNFINISHED
    assert document["exchanges"][0]["incomplete_reason"] == measure.UNFINISHED
    assert "had not finished inspection when the report was written" in text
    assert "inspection incomplete for 1 request(s): 1" in text


def _send_without_waiting(running) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", running.port, timeout=30)
    connection.request("POST", "/v1/messages", BODY)
    connection.getresponse().read()
    connection.close()


def test_a_finished_measurement_leaves_no_reason_behind(provider) -> None:
    provider()
    running = proxy.start("api.anthropic.com", evaluate)
    try:
        _send(running)
    finally:
        running.stop()

    (exchange,) = running.session.exchanges
    assert exchange.measured
    assert exchange.incomplete_reason == ""
    assert exchange.entities == {"EMAIL": 1}


def test_a_request_the_detector_cannot_finish_is_not_called_not_json(
    provider, monkeypatch
) -> None:
    def refuse(_text):
        raise RuntimeError("synthetic detector failure")

    provider()
    running = proxy.start("api.anthropic.com", refuse)
    try:
        _send(running)
    finally:
        running.stop()

    (exchange,) = running.session.exchanges
    assert exchange.incomplete_reason == measure.NOT_ANALYSED


# -- the upstream leg honours the client's own egress proxy --------------------


class _Egress:
    """Records the first request line and headers, then refuses the tunnel."""

    def __init__(self) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.requests: list = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            with client:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.requests.append(data.decode("latin-1").split("\r\n"))
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")

    def stop(self) -> None:
        self.listener.close()


@pytest.fixture
def egress():
    server = _Egress()
    yield server
    server.stop()


def test_the_upstream_leg_tunnels_through_the_clients_https_proxy(
    monkeypatch, egress
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{egress.port}")
    running = proxy.start("api.anthropic.com")
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                urllib.request.Request(
                    running.base_url + "/v1/messages", data=BODY, method="POST"
                ),
                timeout=30,
            )
        running.session.drain(10)
    finally:
        running.stop()

    assert caught.value.code == 502
    # http.client writes this line itself: HTTP/1.0 up to 3.11, the
    # connection's own HTTP/1.1 from 3.12 on.
    version = "HTTP/1.1" if sys.version_info >= (3, 12) else "HTTP/1.0"
    assert egress.requests[0][0] == f"CONNECT api.anthropic.com:443 {version}"
    assert running.session.errors == 1


def test_the_egress_proxy_s_login_is_sent_as_proxy_authorization(
    monkeypatch, egress
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", f"http://user:p%40ss@127.0.0.1:{egress.port}")
    running = proxy.start("api.anthropic.com")
    try:
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(
                urllib.request.Request(
                    running.base_url + "/v1/messages", data=BODY, method="POST"
                ),
                timeout=30,
            )
        running.session.drain(10)
    finally:
        running.stop()

    # base64 of "user:p@ss"
    assert "Proxy-Authorization: Basic dXNlcjpwQHNz" in egress.requests[0]


def test_no_proxy_keeps_the_upstream_leg_direct(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "api.anthropic.com")

    assert proxy._egress("api.anthropic.com") is None


def test_without_a_configured_proxy_the_upstream_leg_is_direct() -> None:
    assert proxy._egress("api.anthropic.com") is None


def test_a_system_proxy_the_client_does_not_read_is_ignored(monkeypatch) -> None:
    # Claude Code reads HTTPS_PROXY, not macOS's network settings.
    monkeypatch.setattr(
        urllib.request, "getproxies", lambda: {"https": "http://127.0.0.1:9"}
    )

    assert proxy._egress("api.anthropic.com") is None


@pytest.mark.parametrize(
    "address", ["https://proxy.invalid:3128", "socks5://127.0.0.1:1080"]
)
def test_an_egress_proxy_shim_cannot_tunnel_through_refuses_to_start(
    monkeypatch, address
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", address)

    with pytest.raises(OSError, match="egress proxies"):
        proxy.start("api.anthropic.com")
