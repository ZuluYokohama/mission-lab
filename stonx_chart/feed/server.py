"""Loopback HTTP server and command line of the StonX chart feed.

The server binds `127.0.0.1` only and answers GET only (every other method gets 405):

    GET /api/stream?asset=SYM   server-sent events of one asset: a snapshot, then live updates
    GET /api/health             {"source": ..., "feed_state": ...}
    GET /*                      static files of the built web app (`STATIC_DIR`)

Threads. `ThreadingHTTPServer` runs each connection on its own daemon thread, and a stream's
writer thread is the only user of its `ClientChannel` and `Envelope`. One pump thread
(`run_pump`) drives the hub every 50 ms. A writer never blocks the pump: a slow client's
channel overflows and the writer resyncs it with a fresh snapshot (design spec section 6).

A stream sends the snapshot, then a `status` (so a connection opened while the feed is
degraded learns it at once), then everything its channel yields, a heartbeat every second,
and after an overflow a new snapshot, again followed by a `status`. Standard library only.
"""

import argparse
import json
import math
import re
import select
import socket
import sys
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from stonx_chart.feed.hub import StreamHub
from stonx_chart.feed.records import asset_id
from stonx_chart.feed.sources import MockFeedSource, SimClock
from stonx_chart.feed.wire import (
    SOURCE_SIMULATED,
    Envelope,
    Payload,
    encode_sse,
    heartbeat_payload,
    ns_to_ms,
    status_payload,
)

STATIC_DIR = Path(__file__).resolve().parents[1] / "web" / "dist"
LOOPBACK_HOST = "127.0.0.1"

_HEARTBEAT_S = 1.0
_TAKE_TIMEOUT_S = 0.25  # a writer checks its client and the heartbeat clock this often
_MAX_DISCARD_BYTES = 1 << 20  # the largest request body read and thrown away before a 405
_NS_PER_S = 1_000_000_000
_DURATION = re.compile(r"(?:([0-9]+)h)?(?:([0-9]+)m)?(?:([0-9]+)s)?")

# A fixed table, not `mimetypes`: the host database varies (the Windows registry may map
# `.js` to text/plain, and Linux /etc/mime.types maps `.xyz` to chemical/x-xyz).
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
}


class _FeedServer(ThreadingHTTPServer):
    daemon_threads = True  # a stream writer must never keep the process alive
    # On Windows SO_REUSEADDR lets a second server bind a port that is already being served.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address: tuple[str, int], hub: StreamHub, static_dir: Path) -> None:
        self.hub = hub
        self.static_dir = static_dir
        super().__init__(address, _Handler)


def _resolve_static(static_dir: Path, url_path: str) -> Path | None:
    """The file `url_path` names inside `static_dir`, or None if it is missing or lies outside.

    The path is checked as text before the file system is touched: no `..`, no drive,
    separator or stream syntax, so nothing the client sends can name a place outside the
    directory (or a network share on Windows). Resolving the result then also catches a
    symbolic link that points out of it.
    """
    parts = [part for part in unquote(url_path).split("/") if part not in ("", ".")]
    if any(part == ".." or "\\" in part or ":" in part or "\0" in part for part in parts):
        return None
    root = static_dir.resolve()
    try:
        candidate = root.joinpath(*(parts or ["index.html"])).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            return candidate
    except (OSError, ValueError):
        pass
    return None


def _content_type(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


class _Handler(BaseHTTPRequestHandler):
    server: _FeedServer

    def log_message(self, format: str, *args) -> None:
        pass  # a health poller would fill the console; unexpected errors still reach stderr

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/api/stream":
            self._stream(parse_qs(url.query).get("asset", [""])[0])
        elif url.path == "/api/health":
            self._send_json(
                HTTPStatus.OK, {"source": SOURCE_SIMULATED, "feed_state": self.server.hub.feed_state}
            )
        else:
            self._send_static(url.path)

    def __getattr__(self, name: str):
        # `BaseHTTPRequestHandler` dispatches a request to `do_<METHOD>`, or answers 501 if there
        # is none. Only GET is served, so every other method, standard or not, gets a 405.
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def _method_not_allowed(self) -> None:
        self._discard_request_body()
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _discard_request_body(self) -> None:
        """Read what the client sent along. Closing a socket with unread data resets the
        connection on Windows, and the client then never sees our response."""
        try:
            remaining = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return
        if remaining > _MAX_DISCARD_BYTES:
            return
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return
            remaining -= len(chunk)

    # ---- plain responses ---------------------------------------------------------------

    def _send_json(self, status: HTTPStatus, body: Payload) -> None:
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _send_static(self, url_path: str) -> None:
        path = _resolve_static(self.server.static_dir, url_path)
        try:
            body = None if path is None else path.read_bytes()
        except OSError:
            body = None
        if body is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", _content_type(path))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- the stream ---------------------------------------------------------------------

    def _stream(self, symbol: str) -> None:
        try:
            asset = asset_id(symbol)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "unknown asset"})
            return
        hub = self.server.hub
        channel, snapshot = hub.subscribe(asset)
        try:
            # Small writes must not wait for an ACK: a tick should reach the screen within 100 ms.
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            envelope = Envelope(SOURCE_SIMULATED)

            def send(*payloads: Payload) -> None:
                self.wfile.write(b"".join(encode_sse(envelope.wrap(p)) for p in payloads))

            def send_snapshot(snap: Payload) -> None:
                feed_state, message = hub.status()
                send(snap, status_payload(feed_state, channel.discarded, message))

            send_snapshot(snapshot)
            last_beat = time.monotonic()
            while not self._client_closed():
                messages = channel.take(_TAKE_TIMEOUT_S)
                if channel.needs_snapshot:
                    send_snapshot(hub.resync(channel))
                elif messages:
                    send(*messages)
                now = time.monotonic()
                if now - last_beat >= _HEARTBEAT_S:
                    send(heartbeat_payload(ns_to_ms(time.time_ns())))
                    last_beat = now
        except ConnectionError:
            pass  # the client went away: BrokenPipeError, ConnectionResetError, ConnectionAbortedError
        finally:
            hub.unsubscribe(channel)

    def _client_closed(self) -> bool:
        """Whether the client has closed its end. A browser sends nothing after its request.

        A write only fails a heartbeat or two after the client left; this notices at once,
        so an idle stream is dropped within one `take` timeout.
        """
        try:
            if not select.select([self.connection], [], [], 0)[0]:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except ConnectionError:
            return True


def make_server(hub: StreamHub, host: str, port: int, static_dir: Path) -> ThreadingHTTPServer:
    """A server for `hub` on `host:port`; `host` must be `127.0.0.1`. Port 0 picks a free port."""
    if host != LOOPBACK_HOST:
        raise ValueError(f"the feed only serves loopback ({LOOPBACK_HOST}), not {host!r}")
    return _FeedServer((host, port), hub, static_dir)


def run_pump(hub: StreamHub, clock: SimClock, stop: threading.Event, period_s: float = 0.05) -> None:
    """Drive the hub from the clock every `period_s` until `stop` is set.

    Any exception from the hub is written to stderr, once per distinct message, and the loop
    carries on: `StreamHub.pump` turns only source errors into a degraded feed, and a bug in
    the derivation must not silently end the pump while heartbeats keep the stream looking alive.
    """
    reported: set[str] = set()
    while not stop.is_set():
        try:
            hub.pump(clock.now_ns())
            hub.flush_all()
        except Exception as e:
            key = f"{type(e).__name__}: {e}"
            if key not in reported:
                reported.add(key)
                print("feed pump failed; it keeps running and shows this error once:", file=sys.stderr)
                traceback.print_exc()
        stop.wait(period_s)


# ---- command line --------------------------------------------------------------------------


def parse_duration(text: str) -> int:
    """Seconds in `text`, written like `90s`, `60m`, `6h30m` or `1h5m30s`. ValueError otherwise."""
    match = _DURATION.fullmatch(text)
    if not text or match is None:
        raise ValueError(f"invalid duration {text!r}: write it like 90s, 60m or 6h30m")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m stonx_chart.feed", description="Serve the mock StonX chart feed on loopback."
    )
    parser.add_argument("--seed", type=int, default=7, help="mock feed seed (default 7)")
    parser.add_argument("--speed", type=float, default=1.0, help="simulated seconds per second (default 1)")
    parser.add_argument("--backfill", default="60m", help="history to generate at start (default 60m)")
    parser.add_argument("--history", default="6h30m", help="rolling window to keep (default 6h30m)")
    parser.add_argument("--port", type=int, default=8765, help="port to listen on (default 8765)")
    parser.add_argument("--host", default=LOOPBACK_HOST, help=f"must be {LOOPBACK_HOST}")
    return parser


def _fail(message: str, code: int = 2) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.host != LOOPBACK_HOST:
        return _fail(f"--host must be {LOOPBACK_HOST}: the feed only serves loopback (got {args.host!r})")
    try:
        backfill_s = parse_duration(args.backfill)
    except ValueError as e:
        return _fail(f"--backfill: {e}")
    try:
        history_s = parse_duration(args.history)
    except ValueError as e:
        return _fail(f"--history: {e}")
    if history_s == 0:
        return _fail("--history must be longer than 0s")
    if not (math.isfinite(args.speed) and args.speed > 0):
        return _fail(f"--speed must be a positive number (got {args.speed})")
    if not 0 <= args.port <= 65535:
        return _fail(f"--port must be between 0 and 65535 (got {args.port})")
    try:
        return _run(args, backfill_s, history_s)
    except KeyboardInterrupt:
        return 0


def _run(args: argparse.Namespace, backfill_s: int, history_s: int) -> int:
    now_ns = time.time_ns()
    start_ns = (now_ns - backfill_s * _NS_PER_S) // _NS_PER_S * _NS_PER_S
    hub = StreamHub(MockFeedSource(args.seed, start_ns), start_ns, window_s=history_s)
    # Bind first: a taken port must fail at once, not after the backfill. Nothing is served until
    # `serve_forever`, so a client that connects meanwhile just waits in the listen queue.
    try:
        server = make_server(hub, args.host, args.port, STATIC_DIR)
    except OSError as e:
        return _fail(f"cannot listen on {args.host}:{args.port}: {e}", code=1)
    try:
        hub.pump(now_ns)
    except BaseException:
        server.server_close()
        raise
    if not (STATIC_DIR / "index.html").is_file():
        print(
            "warning: web/dist not built - run npm run build in stonx_chart/web "
            "(the API still works; the page will 404)",
            file=sys.stderr,
            flush=True,
        )
    stop = threading.Event()
    pump = threading.Thread(
        target=run_pump, args=(hub, SimClock(now_ns, args.speed), stop), name="feed-pump", daemon=True
    )
    pump.start()
    port = server.server_address[1]
    print(f"StonX chart feed ({SOURCE_SIMULATED}) on http://{args.host}:{port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        pump.join(timeout=2.0)
    return 0
