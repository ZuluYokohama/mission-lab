import http.client
import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from stonx_chart.feed import hub as hub_module
from stonx_chart.feed import server as server_module
from stonx_chart.feed.hub import ClientChannel, StreamHub
from stonx_chart.feed.server import STATIC_DIR, main, make_server, parse_duration, run_pump
from stonx_chart.feed.sources import MockFeedSource, SimClock
from stonx_chart.feed.tests.factories import START_NS

S = 1_000_000_000
REPO_ROOT = Path(__file__).resolve().parents[3]


# ---- helpers ------------------------------------------------------------------------------


def wait_until(predicate: Callable[[], object], timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def fetch(port: int, path: str, method: str = "GET", body: bytes | None = None):
    """One request on its own connection; returns the response with its body already read."""
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)) as conn:
        conn.request(method, path, body=body)
        response = conn.getresponse()
        response.body = response.read()
        return response


@contextmanager
def open_stream(port: int, query: str = "asset=NVDA") -> Iterator[http.client.HTTPResponse]:
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)) as conn:
        conn.request("GET", f"/api/stream?{query}")
        # A response that will close the connection takes over the socket, so closing the
        # connection alone would leave the stream open.
        with closing(conn.getresponse()) as response:
            yield response


def next_message(response: http.client.HTTPResponse) -> dict:
    """The next server-sent event, decoded."""
    while True:
        line = response.readline()
        assert line, "the stream ended"
        if line.startswith(b"data: "):
            return json.loads(line[len(b"data: "):])


class FailingSource:
    drop_count = 0

    def advance(self, until_ns: int) -> list:
        raise RuntimeError("boom")


class RecordingHub:
    """Stands in for `StreamHub` in `run_pump`: records calls and raises the queued failures."""

    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.calls: list[tuple] = []
        self._failures = list(failures or [])

    def pump(self, until_ns: int) -> None:
        self.calls.append(("pump", until_ns))
        if self._failures:
            raise self._failures.pop(0)

    def flush_all(self) -> None:
        self.calls.append(("flush",))


class CountingClock:
    def __init__(self) -> None:
        self._now = 0

    def now_ns(self) -> int:
        self._now += 1
        return self._now


@pytest.fixture
def hub() -> StreamHub:
    hub = StreamHub(MockFeedSource(7, START_NS), START_NS)
    hub.pump(START_NS + 30 * S)
    return hub


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    path = tmp_path / "dist"
    path.mkdir()
    return path


@pytest.fixture
def serve(tmp_path: Path):
    """`serve(hub, static_dir=None)` runs a server on a free loopback port and returns the port.

    At teardown every server is stopped and the test fails if a stream writer or any other
    thread outlived its connection.
    """
    running: list[tuple[StreamHub, ThreadingHTTPServer, threading.Thread]] = []
    threads_before = set(threading.enumerate())

    def start(hub: StreamHub, static_dir: Path | None = None) -> int:
        server = make_server(hub, "127.0.0.1", 0, static_dir or tmp_path)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, name="test-server"
        )
        thread.start()
        running.append((hub, server, thread))
        return server.server_address[1]

    yield start

    for _, server, thread in running:
        server.shutdown()
        thread.join(5.0)
        server.server_close()
    for hub, _, _ in running:
        assert wait_until(lambda: hub.channel_count() == 0), "a stream writer outlived its client"
    assert wait_until(lambda: set(threading.enumerate()) <= threads_before), "threads leaked"


@pytest.fixture
def no_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Makes building the feed source fail, to prove that `main` validated its arguments first."""

    def refuse(*args, **kwargs):
        raise AssertionError("main built the source before it validated its arguments")

    monkeypatch.setattr(server_module, "MockFeedSource", refuse)


# ---- make_server ------------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "::1", "192.168.1.5", ""])
def test_make_server_rejects_non_loopback(hub, tmp_path, host):
    with pytest.raises(ValueError):
        make_server(hub, host, 0, tmp_path)


def test_make_server_binds_loopback_with_daemon_threads(hub, tmp_path):
    server = make_server(hub, "127.0.0.1", 0, tmp_path)
    try:
        assert isinstance(server, ThreadingHTTPServer)
        assert server.server_address[0] == "127.0.0.1"
        assert server.daemon_threads is True
    finally:
        server.server_close()


def test_a_second_server_cannot_share_a_port(hub, serve, tmp_path):
    port = serve(hub)
    with pytest.raises(OSError):
        make_server(hub, "127.0.0.1", port, tmp_path)


# ---- routes ---------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/stream?asset=NVDA", "/"])
@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT", "PROPFIND"]
)
def test_non_get_405(hub, serve, method, path):
    response = fetch(serve(hub), path, method)
    assert response.status == 405
    assert response.getheader("Allow") == "GET"
    assert hub.channel_count() == 0


def test_405_with_a_request_body(hub, serve):
    response = fetch(serve(hub), "/api/stream?asset=NVDA", "POST", body=b"x" * 200_000)
    assert response.status == 405 and response.getheader("Allow") == "GET"


def test_health(hub, serve):
    response = fetch(serve(hub), "/api/health")
    assert response.status == 200
    assert response.getheader("Content-Type") == "application/json"
    assert json.loads(response.body) == {"source": "SIMULATED_DEMO", "feed_state": "ok"}


def fetch_with_host(port: int, path: str, host: str):
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)) as conn:
        conn.request("GET", path, headers={"Host": host})
        response = conn.getresponse()
        response.body = response.read()
        return response


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost:5173", "LOCALHOST:8765"])
def test_loopback_host_headers_are_served(hub, serve, host):
    assert fetch_with_host(serve(hub), "/api/health", host).status == 200


@pytest.mark.parametrize(
    "host", ["evil.example", "evil.example:8765", "127.0.0.1.evil.example", "localhost:x", "[::1]:8765", ""]
)
@pytest.mark.parametrize("path", ["/api/health", "/api/stream?asset=NVDA", "/"])
def test_other_host_headers_get_421(hub, serve, host, path):
    response = fetch_with_host(serve(hub), path, host)
    assert response.status == 421
    assert json.loads(response.body) == {"error": "host not allowed"}


def test_health_reports_a_degraded_feed(serve):
    degraded = StreamHub(FailingSource(), START_NS)
    degraded.pump(START_NS + S)
    response = fetch(serve(degraded), "/api/health")
    assert json.loads(response.body) == {"source": "SIMULATED_DEMO", "feed_state": "degraded"}


@pytest.mark.parametrize("query", ["asset=TSLA", "asset=nvda", "asset=", "other=NVDA", ""])
def test_unknown_asset_400(hub, serve, query):
    response = fetch(serve(hub), f"/api/stream?{query}")
    assert response.status == 400
    assert response.getheader("Content-Type") == "application/json"
    assert json.loads(response.body) == {"error": "unknown asset"}
    assert hub.channel_count() == 0


# ---- the stream -----------------------------------------------------------------------------


def test_stream_snapshot_first_then_heartbeat(hub, serve):
    port = serve(hub)
    with open_stream(port) as response:
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream"
        assert response.getheader("Cache-Control") == "no-cache"

        snapshot = next_message(response)
        assert (snapshot["v"], snapshot["type"], snapshot["seq"]) == (1, "snapshot", 1)
        assert snapshot["source"] == "SIMULATED_DEMO" and snapshot["asset"] == "NVDA"
        assert snapshot["bars"]["t"], "the snapshot should carry the backfilled bars"

        assert next_message(response) == {
            "v": 1, "type": "status", "source": "SIMULATED_DEMO", "seq": 2,
            "feed_state": "ok", "drop_count": 0,
        }

        started = time.monotonic()
        first = next_message(response)
        elapsed = time.monotonic() - started
        assert (first["type"], first["seq"], first["source"]) == ("heartbeat", 3, "SIMULATED_DEMO")
        assert 0.9 <= elapsed < 2.0
        assert abs(first["now_ms"] - time.time() * 1000) < 5000

        second = next_message(response)
        assert (second["type"], second["seq"]) == ("heartbeat", 4)
        assert second["now_ms"] - first["now_ms"] >= 900


def test_stream_serves_the_requested_asset(hub, serve):
    with open_stream(serve(hub), "asset=AMD") as response:
        assert next_message(response)["asset"] == "AMD"


def test_stream_follows_a_degraded_snapshot_with_the_degraded_status(serve):
    degraded = StreamHub(FailingSource(), START_NS)
    degraded.pump(START_NS + S)
    with open_stream(serve(degraded)) as response:
        assert next_message(response)["type"] == "snapshot"
        status = next_message(response)
        assert status["type"] == "status" and status["seq"] == 2
        assert status["feed_state"] == "degraded" and status["message"] == "RuntimeError: boom"


def test_stream_delivers_live_updates_in_sequence(hub, serve):
    port = serve(hub)
    stop = threading.Event()
    pump = threading.Thread(target=run_pump, args=(hub, SimClock(START_NS + 30 * S, 20.0), stop))
    with open_stream(port) as response:
        messages = [next_message(response), next_message(response)]
        pump.start()
        try:
            for _ in range(300):
                messages.append(next_message(response))
                if {"bar", "series.points"} <= {m["type"] for m in messages}:
                    break
            assert {"bar", "series.points"} <= {m["type"] for m in messages}
        finally:
            stop.set()
            pump.join(5.0)
        assert not pump.is_alive()
    assert [m["seq"] for m in messages] == list(range(1, len(messages) + 1))
    assert {m["v"] for m in messages} == {1}  # no payload key may replace the envelope's version
    points = next(m for m in messages if m["type"] == "series.points")
    assert list(points) == ["v", "type", "source", "seq", "id", "points"]
    assert list(points["points"]) == ["t", "v"]
    assert [len(points["points"]["t"]), len(points["points"]["v"])] == [1, 1]


def test_stream_resyncs_after_overflow(hub, serve, monkeypatch):
    # A one-message queue overflows as soon as a flush queues two held updates.
    monkeypatch.setattr(hub_module, "ClientChannel", partial(ClientChannel, max_queue=1))
    port = serve(hub)
    with open_stream(port) as response:
        first = next_message(response)
        assert next_message(response)["type"] == "status"

        hub.pump(START_NS + 31 * S)
        hub.flush_all()

        for _ in range(200):
            resync = next_message(response)
            if resync["type"] == "snapshot":
                break
        assert resync["type"] == "snapshot"
        assert resync["bars"]["t"][-1] > first["bars"]["t"][-1]
        status = next_message(response)
        assert status["type"] == "status" and status["seq"] == resync["seq"] + 1
        assert status["drop_count"] > 0 and status["feed_state"] == "ok"


def test_disconnect_unsubscribes(hub, serve):
    port = serve(hub)
    with open_stream(port) as response:
        assert next_message(response)["type"] == "snapshot"
        assert hub.channel_count() == 1
    assert wait_until(lambda: hub.channel_count() == 0, 2.0)


def test_many_streams_on_one_asset_are_independent(hub, serve):
    port = serve(hub)
    with open_stream(port) as one, open_stream(port) as two:
        assert next_message(one)["seq"] == 1
        assert next_message(two)["seq"] == 1
        assert hub.channel_count() == 2
    assert wait_until(lambda: hub.channel_count() == 0, 2.0)


# ---- static files -----------------------------------------------------------------------------


def test_static_files(hub, serve, static_dir):
    (static_dir / "index.html").write_text("<h1>chart</h1>", encoding="utf-8")
    (static_dir / "app.js").write_text("export const a = 1;", encoding="utf-8")
    (static_dir / "app.css").write_text("body{}", encoding="utf-8")
    (static_dir / "icon.svg").write_text("<svg/>", encoding="utf-8")
    (static_dir / "notes.xyz").write_bytes(b"\x00\x01")
    (static_dir / "assets").mkdir()
    (static_dir / "assets" / "chunk.js").write_text("export {};", encoding="utf-8")
    port = serve(hub, static_dir)

    for path, content_type, body in [
        ("/", "text/html; charset=utf-8", b"<h1>chart</h1>"),
        ("/index.html", "text/html; charset=utf-8", b"<h1>chart</h1>"),
        ("/app.js", "text/javascript", b"export const a = 1;"),
        ("/app.js?v=3", "text/javascript", b"export const a = 1;"),
        ("/assets/chunk.js", "text/javascript", b"export {};"),
        ("/app.css", "text/css", b"body{}"),
        ("/icon.svg", "image/svg+xml", b"<svg/>"),
        ("/notes.xyz", "application/octet-stream", b"\x00\x01"),
    ]:
        response = fetch(port, path)
        assert response.status == 200, path
        assert response.getheader("Content-Type") == content_type, path
        assert response.getheader("Content-Length") == str(len(body)), path
        assert response.body == body, path


@pytest.mark.parametrize("path", ["/missing.js", "/assets", "/assets/", "/nope/index.html", "/api/other"])
def test_static_missing_404(hub, serve, static_dir, path):
    (static_dir / "assets").mkdir()
    assert fetch(serve(hub, static_dir), path).status == 404


def test_static_index_missing_404(hub, serve, static_dir):
    assert fetch(serve(hub, static_dir), "/").status == 404


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/..%2fsecret.txt",
        "/sub/../../secret.txt",
        "/..%5csecret.txt",
        "/%5c..%5csecret.txt",
        "//..//secret.txt",
        "/./../secret.txt",
        "/%00/../secret.txt",
    ],
)
def test_static_traversal_404(hub, serve, static_dir, tmp_path, path):
    (static_dir / "index.html").write_text("<h1>chart</h1>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    response = fetch(serve(hub, static_dir), path)
    assert response.status == 404
    assert b"secret" not in response.body


def test_static_absolute_path_404(hub, serve, static_dir, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    response = fetch(serve(hub, static_dir), "/" + secret.as_posix())
    assert response.status == 404 and b"secret" not in response.body


def test_static_symlink_out_of_the_directory_404(hub, serve, static_dir, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    try:
        (static_dir / "link.txt").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    response = fetch(serve(hub, static_dir), "/link.txt")
    assert response.status == 404 and b"secret" not in response.body


def test_static_dir_defaults_to_the_built_web_app():
    assert STATIC_DIR == REPO_ROOT / "stonx_chart" / "web" / "dist"


# ---- run_pump ---------------------------------------------------------------------------------


def test_run_pump_pumps_then_flushes_until_stopped():
    hub, stop = RecordingHub(), threading.Event()
    thread = threading.Thread(target=run_pump, args=(hub, CountingClock(), stop, 0.001))
    thread.start()
    assert wait_until(lambda: len(hub.calls) >= 6)
    stop.set()
    thread.join(5.0)
    assert not thread.is_alive()
    calls = list(hub.calls)
    assert calls[:6] == [("pump", 1), ("flush",), ("pump", 2), ("flush",), ("pump", 3), ("flush",)]


def test_run_pump_stops_without_waiting_out_its_period():
    hub, stop = RecordingHub(), threading.Event()
    thread = threading.Thread(target=run_pump, args=(hub, CountingClock(), stop, 60.0))
    thread.start()
    assert wait_until(lambda: len(hub.calls) >= 2)
    stop.set()
    thread.join(2.0)
    assert not thread.is_alive()


def test_run_pump_survives_errors_and_reports_each_message_once(capsys):
    failures = [RuntimeError("boom"), RuntimeError("boom"), ValueError("other"), RuntimeError("boom")]
    hub, stop = RecordingHub(failures), threading.Event()
    thread = threading.Thread(target=run_pump, args=(hub, CountingClock(), stop, 0.001))
    thread.start()
    assert wait_until(lambda: ("flush",) in hub.calls)  # the first iteration that did not raise
    assert wait_until(lambda: len(hub.calls) >= 12)
    stop.set()
    thread.join(5.0)
    assert not thread.is_alive()

    err = capsys.readouterr().err
    assert err.count("RuntimeError: boom") == 1
    assert err.count("ValueError: other") == 1
    assert err.count("Traceback (most recent call last)") == 2
    pumps = [call for call in hub.calls if call[0] == "pump"]
    assert len(pumps) >= 6 and hub.calls.count(("flush",)) >= 2  # it carried on after the failures


def test_run_pump_drives_a_real_hub():
    hub = StreamHub(MockFeedSource(7, START_NS), START_NS)
    channel, _ = hub.subscribe(2)
    stop = threading.Event()
    thread = threading.Thread(target=run_pump, args=(hub, SimClock(START_NS, 50.0), stop, 0.01))
    thread.start()
    try:
        messages: list[dict] = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not {"bar", "series.points"} <= {m["type"] for m in messages}:
            messages += channel.take(0.1)
        assert {"bar", "series.points"} <= {m["type"] for m in messages}
    finally:
        stop.set()
        thread.join(5.0)
        hub.unsubscribe(channel)
    assert not thread.is_alive()


# ---- parse_duration -----------------------------------------------------------------------


def test_parse_duration():
    assert [parse_duration(x) for x in ("90s", "60m", "6h30m", "0s")] == [90, 3600, 23400, 0]
    assert [parse_duration(x) for x in ("1m30s", "2h", "1h1m1s", "00005s")] == [90, 7200, 3661, 5]


@pytest.mark.parametrize(
    "text", ["5x", "", "5", "h", "1h2", "-5s", "1.5h", "5 s", " 5s", "5s ", "30m6h", "5S", "1h 30m", "m5"]
)
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        parse_duration(text)


# ---- main ---------------------------------------------------------------------------------------


def test_rejects_non_loopback(hub, tmp_path, no_source, capsys):
    with pytest.raises(ValueError):
        make_server(hub, "0.0.0.0", 0, tmp_path)
    assert main(["--host", "0.0.0.0"]) == 2
    err = capsys.readouterr().err
    assert "127.0.0.1" in err and "0.0.0.0" in err


@pytest.mark.parametrize(
    "flags",
    [
        ["--backfill", "5x"],
        ["--backfill", ""],
        ["--history", "abc"],
        ["--history", "0s"],
        ["--speed", "0"],
        ["--speed", "-1"],
        ["--speed", "nan"],
        ["--speed", "inf"],
        ["--port", "70000"],
        ["--port", "-1"],
    ],
)
def test_main_rejects_bad_arguments_before_building_anything(no_source, capsys, flags):
    assert main(flags) == 2
    assert capsys.readouterr().err.startswith("error: ")


def test_main_reports_a_port_that_is_taken_before_any_backfill(hub, serve, capsys, monkeypatch):
    port = serve(hub)

    def no_backfill(self, until_ns):
        raise AssertionError("main ran the backfill before it bound the port")

    monkeypatch.setattr(StreamHub, "pump", no_backfill)
    assert main(["--backfill", "6h", "--port", str(port)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and str(port) in err


class _StoppedServer:
    """Stands in for the server in `main`: serves nothing and 'stops' at once (as Ctrl-C would)."""

    server_address = ("127.0.0.1", 0)

    def serve_forever(self) -> None:
        raise KeyboardInterrupt

    def server_close(self) -> None:
        pass


@pytest.mark.parametrize("built", [False, True])
def test_main_warns_on_stderr_when_the_web_app_is_not_built(tmp_path, capsys, monkeypatch, built):
    if built:
        (tmp_path / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.setattr(server_module, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(server_module, "make_server", lambda *args, **kwargs: _StoppedServer())
    assert main(["--backfill", "0s", "--port", "0"]) == 0
    captured = capsys.readouterr()
    assert ("web/dist not built" in captured.err) is (not built)
    assert "npm run build" in captured.err or built
    assert "StonX chart feed" in captured.out


def test_cli_serves_the_feed():
    command = [sys.executable, "-B", "-m", "stonx_chart.feed", "--backfill", "10s", "--port", "0"]
    with subprocess.Popen(
        command, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    ) as process:
        watchdog = threading.Timer(60.0, process.kill)
        watchdog.start()
        try:
            banner = process.stdout.readline()
            match = re.fullmatch(
                r"StonX chart feed \(SIMULATED_DEMO\) on http://127\.0\.0\.1:(\d+)/\n", banner
            )
            if match is None:
                process.kill()
                pytest.fail(f"unexpected banner {banner!r}; stderr: {process.stderr.read()}")
            port = int(match.group(1))

            health = fetch(port, "/api/health")
            assert health.status == 200
            assert json.loads(health.body) == {"source": "SIMULATED_DEMO", "feed_state": "ok"}
            with open_stream(port) as response:
                snapshot = next_message(response)
                assert (snapshot["type"], snapshot["seq"], snapshot["asset"]) == ("snapshot", 1, "NVDA")
                assert len(snapshot["bars"]["t"]) >= 9  # ten seconds of backfill
                assert next_message(response)["type"] == "status"
        finally:
            watchdog.cancel()
            process.kill()
