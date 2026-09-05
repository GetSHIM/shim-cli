from __future__ import annotations

import codecs
import http.client
import http.server
import socketserver
import ssl
import threading
import time
import urllib.parse
import zlib
from dataclasses import dataclass, field

from .measure import MAX_BODY_BYTES, Exchange, UsageReader, inspect_request

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
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _measurement_slots: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(2)
    )
    _in_flight: int = 0
    _idle: threading.Event = field(default_factory=threading.Event)

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
        measuring = (
            length <= MAX_BODY_BYTES and self.session._measurement_slots.acquire(False)
        )

        def body():
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
                yield chunk

        connection = None
        responded = False
        try:
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in HOP_BY_HOP
                and name.lower() not in {"host", "content-length"}
            }
            headers["Host"] = self.upstream_host
            headers["Content-Length"] = str(length)
            connection = http.client.HTTPSConnection(
                self.upstream_host,
                timeout=UPSTREAM_TIMEOUT_SECONDS,
                context=self.tls_context,
            )
            connection.request(self.command, self.path, body=body(), headers=headers)
            upstream = connection.getresponse()
            exchange.status = upstream.status
            responded = True
            self.session.record(exchange)
            self._stream(upstream, exchange)
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
            if connection is not None:
                connection.close()
            if measuring:
                self.session._measurement_slots.release()

    def _measure(self, body: bytes | bytearray, exchange: Exchange) -> None:
        try:
            measured = inspect_request(body, self.evaluate)
        except Exception:
            exchange.measured = False
            return
        exchange.model = measured.model
        exchange.sections = measured.sections
        exchange.entities = measured.entities
        exchange.at_files = measured.at_files
        exchange.measured = measured.measured

    def _stream(self, upstream, exchange: Exchange) -> None:
        self.send_response(upstream.status)
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
            except (UnicodeError, ValueError, zlib.error):
                readable = False
        readable = readable and (decoder is None or decoder.eof)
        if readable:
            try:
                reader.feed(text_decoder.decode(b"", final=True))
                reader.finish()
            except UnicodeError:
                readable = False
        exchange.usage_status = reader.status if readable else "unavailable"
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            pass
        exchange.usage = reader.usage

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
        },
    )
    # Loopback carries live credentials.
    server = _Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return Watch(server.server_address[1], session, server, thread)


__all__ = ["CHUNK_BYTES", "HOP_BY_HOP", "Session", "Watch", "start"]
