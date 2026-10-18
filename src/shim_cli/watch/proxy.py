from __future__ import annotations

import base64
import codecs
import hashlib
import http.client
import http.server
import socketserver
import ssl
import threading
import time
import urllib.parse
import urllib.request
import zlib
from collections import Counter
from dataclasses import dataclass, field

from .measure import (
    BODY_TOO_LARGE,
    MAX_BODY_BYTES,
    MODEL_PATHS,
    NOT_ANALYSED,
    SLOTS_BUSY,
    UNFINISHED,
    Exchange,
    SectionMemo,
    UsageReader,
    inspect_request,
    scan_response,
)

# Preserve provider auth headers.
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

# `read` would buffer SSE.
CHUNK_BYTES = 65_536
UPSTREAM_TIMEOUT_SECONDS = 900
DOWNSTREAM_TIMEOUT_SECONDS = 30


@dataclass
class Session:
    exchanges: list = field(default_factory=list)
    errors: int = 0
    memo: SectionMemo = field(default_factory=SectionMemo)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _measurement_slots: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(2)
    )
    _in_flight: int = 0
    _idle: threading.Event = field(default_factory=threading.Event)
    # (path, body digest) of requests still waiting for or streaming their
    # answer, counted; held in memory for the life of the exchange only.
    _pending_bodies: Counter = field(default_factory=Counter)
    # What every measured request carried, once however often history replays
    # it: image digest -> whether its size was read, and tool_use id -> (tool,
    # bytes, requests that carried it at that size). In memory only.
    images: dict = field(default_factory=dict)
    tool_results: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._idle.set()

    def began(self) -> None:
        with self._lock:
            self._in_flight += 1
            self._idle.clear()

    def ended(self) -> None:
        with self._lock:
            self._in_flight -= 1
            if not self._in_flight:
                self._idle.set()

    def drain(self, timeout: float) -> bool:
        return self._idle.wait(timeout)

    def entered(self, key: tuple) -> bool:
        """True when an identical request is still in flight."""
        with self._lock:
            self._pending_bodies[key] += 1
            return self._pending_bodies[key] > 1

    def left(self, key: tuple) -> None:
        with self._lock:
            self._pending_bodies[key] -= 1
            if not self._pending_bodies[key]:
                del self._pending_bodies[key]

    def absorb(self, measured: Exchange) -> None:
        """Fold one request's images and tool results into the session's."""
        with self._lock:
            self.images.update(measured.images)
            for key, (tool, size) in measured.tool_results.items():
                _, seen, carried = self.tool_results.get(key, (tool, 0, 0))
                if size > seen:
                    self.tool_results[key] = (tool, size, 1)
                elif size == seen:
                    self.tool_results[key] = (tool, size, carried + 1)

    def record(self, exchange: Exchange) -> None:
        with self._lock:
            self.exchanges.append(exchange)

    def failed(self) -> None:
        with self._lock:
            self.errors += 1


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    upstream_host = "api.anthropic.com"
    session: Session
    tls_context: ssl.SSLContext
    evaluate = None
    # (host, port, CONNECT headers) of the egress proxy the client would use.
    egress: tuple | None = None

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(DOWNSTREAM_TIMEOUT_SECONDS)

    def log_message(self, *_args: object, **_kwargs: object) -> None:
        pass

    def _relay(self) -> None:
        self.session.began()
        try:
            self._forward()
        finally:
            self.session.ended()

    def _forward(self) -> None:
        self.close_connection = True
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding") or len(lengths) > 1:
            self.send_error(400, "unsupported or ambiguous request framing")
            return
        value = lengths[0].strip(" \t") if lengths else "0"
        if not value.isascii() or not value.isdecimal() or len(value) > 20:
            self.send_error(400, "invalid Content-Length")
            return
        length = int(value)
        exchange = Exchange(
            path=urllib.parse.urlsplit(self.path).path,
            request_bytes=length,
            measured=False,
        )
        capture = bytearray()
        oversized = length > MAX_BODY_BYTES
        measuring = not oversized and self.session._measurement_slots.acquire(False)
        # Recorded before `_measure` runs, so a report written meanwhile says why.
        exchange.incomplete_reason = (
            UNFINISHED if measuring else BODY_TOO_LARGE if oversized else SLOTS_BUSY
        )
        digest = (
            hashlib.sha256()
            if self.command == "POST" and exchange.path.endswith(MODEL_PATHS)
            else None
        )
        pending: tuple = ()

        def body():
            nonlocal pending
            deadline = time.monotonic() + DOWNSTREAM_TIMEOUT_SECONDS
            remaining = length
            while remaining:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise TimeoutError("request body deadline exceeded")
                self.connection.settimeout(timeout)
                chunk = self.rfile.read1(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError("truncated request body")
                remaining -= len(chunk)
                if measuring:
                    capture.extend(chunk)
                if digest is not None:
                    digest.update(chunk)
                yield chunk
            if digest is not None:
                pending = (exchange.path, digest.hexdigest())
                exchange.duplicate_in_flight = self.session.entered(pending)

        connection = None
        responded = False
        try:
            # Pairs, not a dict: a repeated header is forwarded as sent.
            headers = [
                (name, value)
                for name, value in self.headers.items()
                if name.lower() not in HOP_BY_HOP
                and name.lower() not in {"host", "content-length"}
            ]
            names = {name.lower() for name, _ in headers}
            exchange.auth_route = (
                "api-key"
                if "x-api-key" in names
                else "subscription"
                if "authorization" in names
                else ""
            )
            headers += [("Host", self.upstream_host), ("Content-Length", str(length))]
            if self.egress is None:
                connection = http.client.HTTPSConnection(
                    self.upstream_host,
                    timeout=UPSTREAM_TIMEOUT_SECONDS,
                    context=self.tls_context,
                )
            else:
                proxy_host, proxy_port, proxy_headers = self.egress
                connection = http.client.HTTPSConnection(
                    proxy_host,
                    proxy_port,
                    timeout=UPSTREAM_TIMEOUT_SECONDS,
                    context=self.tls_context,
                )
                connection.set_tunnel(self.upstream_host, headers=proxy_headers)
            connection.putrequest(
                self.command,
                self.path,
                skip_host=True,
                skip_accept_encoding="accept-encoding" in names,
            )
            for name, value in headers:
                connection.putheader(name, value)
            connection.endheaders(body())
            upstream = connection.getresponse()
            exchange.status = upstream.status
            responded = True
            self.session.record(exchange)
            self._stream(upstream, exchange, measuring)
            if measuring:
                self._measure(capture, exchange)
        except (OSError, ValueError, http.client.HTTPException):
            self.session.failed()
            if not responded:
                try:
                    self.send_error(502, "request could not be forwarded")
                except OSError:
                    pass
        finally:
            if pending:
                self.session.left(pending)
            if connection is not None:
                connection.close()
            if measuring:
                self.session._measurement_slots.release()

    def _measure(self, body: bytes | bytearray, exchange: Exchange) -> None:
        try:
            measured = inspect_request(body, self.evaluate, self.session.memo)
        except Exception:
            exchange.measured = False
            exchange.incomplete_reason = NOT_ANALYSED
            return
        exchange.model = measured.model or exchange.model
        exchange.conversation = measured.conversation
        exchange.sections = measured.sections
        exchange.entities = measured.entities
        exchange.entities_by_section = measured.entities_by_section
        exchange.custom = measured.custom
        exchange.at_files = measured.at_files
        exchange.image_count = measured.image_count
        exchange.estimated_image_tokens = measured.estimated_image_tokens
        if exchange.path.endswith(MODEL_PATHS):
            self.session.absorb(measured)
        exchange.measured = measured.measured
        exchange.incomplete_reason = measured.incomplete_reason

    def _stream(self, upstream, exchange: Exchange, measuring: bool = False) -> None:
        # Not send_response: it adds a Server and a Date of its own.
        self.send_response_only(upstream.status)
        for name, value in upstream.getheaders():
            lowered = name.lower()
            # Stream without deriving a length.
            if lowered in HOP_BY_HOP or lowered == "content-length":
                continue
            safe_name = name.replace("\n", "").replace("\r", "")
            safe_value = value.replace("\n", "").replace("\r", "")
            if safe_name != name or safe_value != value:
                continue
            self.send_header(safe_name, safe_value)
        self.send_header("Connection", "close")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        reader = UsageReader(upstream.getheader("Content-Type") or "")
        text_decoder = codecs.getincrementaldecoder("utf-8")()
        decoded_bytes = 0
        # Relay compressed bytes unchanged.
        encoding = (upstream.getheader("Content-Encoding") or "").lower()
        decoder = None
        if encoding == "gzip":
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            decoder = zlib.decompressobj()

        readable = encoding in ("", "gzip", "deflate")
        while True:
            try:
                chunk = upstream.read1(CHUNK_BYTES)
            except Exception:
                self.session.failed()
                exchange.usage = reader.usage
                exchange.stop_reason = reader.stop_reason
                reader.forget()
                exchange.usage_status = (
                    "partial" if reader.status != "unavailable" else "unavailable"
                )
                self.close_connection = True
                return
            if not chunk:
                break
            try:
                self.wfile.write(b"%x\r\n" % len(chunk))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            except OSError:
                return
            if not readable:
                continue
            try:
                decoded = (
                    chunk
                    if decoder is None
                    else decoder.decompress(chunk, MAX_BODY_BYTES - decoded_bytes + 1)
                )
                while decoder is not None and decoder.eof and decoder.unused_data:
                    leftover = decoder.unused_data
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    decoded += decoder.decompress(
                        leftover,
                        max(1, MAX_BODY_BYTES - decoded_bytes - len(decoded) + 1),
                    )
                    if len(decoded) + decoded_bytes > MAX_BODY_BYTES:
                        break
                decoded_bytes += len(decoded)
                if decoded_bytes > MAX_BODY_BYTES:
                    readable = False
                    continue
                reader.feed(text_decoder.decode(decoded))
            except Exception:
                # Reading is measurement; whatever it hits, the relay goes on.
                readable = False
        readable = readable and (decoder is None or decoder.eof)
        if readable:
            try:
                reader.feed(text_decoder.decode(b"", final=True))
                reader.finish()
            except Exception:
                readable = False
        exchange.usage_status = reader.status if readable else "unavailable"
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            pass
        exchange.usage = reader.usage
        exchange.stop_reason = reader.stop_reason
        exchange.model = exchange.model or reader.model
        # The client has its last byte; only now does the detector run.
        if measuring and self.evaluate is not None:
            exchange.response_scan_status = (
                reader.response_status if readable else "unavailable"
            )
            if exchange.response_scan_status != "unavailable":
                try:
                    exchange.response_entities, exchange.response_custom, partial = (
                        scan_response(reader, self.evaluate)
                    )
                    if partial:
                        exchange.response_scan_status = NOT_ANALYSED
                except Exception:
                    exchange.response_entities = {}
                    exchange.response_scan_status = "unavailable"
        reader.forget()

    do_POST = _relay
    do_GET = _relay
    do_PUT = _relay
    do_DELETE = _relay
    do_PATCH = _relay


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        pass


@dataclass
class Watch:
    port: int
    session: Session
    _server: _Server
    _thread: threading.Thread
    _stopped: bool = False

    DRAIN_SECONDS = 5.0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._server.shutdown()
        self.session.drain(self.DRAIN_SECONDS)
        self._server.server_close()
        self._thread.join(timeout=5)


def _egress(host: str) -> tuple | None:
    """The proxy the client's own environment sends `host` through, if any.
    Environment only: Claude Code does not read macOS's system proxy either."""
    address = urllib.request.getproxies_environment().get("https")
    # With an environment proxy set, proxy_bypass reads NO_PROXY only.
    if not address or urllib.request.proxy_bypass(host):
        return None
    parts = urllib.parse.urlsplit(address if "//" in address else f"//{address}")
    if parts.scheme not in ("", "http") or not parts.hostname:
        raise OSError("only http:// egress proxies are supported (HTTPS_PROXY)")
    headers = {}
    if parts.username is not None:
        login = f"{urllib.parse.unquote(parts.username)}:"
        login += urllib.parse.unquote(parts.password or "")
        headers["Proxy-Authorization"] = (
            "Basic " + base64.b64encode(login.encode()).decode()
        )
    return parts.hostname, parts.port or 80, headers


def start(upstream: str = "api.anthropic.com", evaluate=None) -> Watch:
    session = Session()
    handler = type(
        "_BoundHandler",
        (_Handler,),
        {
            "upstream_host": upstream,
            "session": session,
            "tls_context": ssl.create_default_context(),
            "evaluate": staticmethod(evaluate) if evaluate else None,
            "egress": _egress(upstream),
        },
    )
    # Loopback carries live credentials.
    server = _Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return Watch(server.server_address[1], session, server, thread)


__all__ = ["CHUNK_BYTES", "HOP_BY_HOP", "Session", "Watch", "start"]
