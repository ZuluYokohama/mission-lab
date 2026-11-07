"""Golden wire fixtures: deterministic NVDA snapshots and a stream, written as the TypeScript contract.

The fixtures come straight from `StreamHub`, with no threads and no HTTP: the hub is pumped one
simulated second at a time and flushed after each second, as the server does every 50 ms but
coarser. A TypeScript contract test decodes the files and checks that snapshot(300 s) plus
stream(300 s to 600 s) gives the same chart state as snapshot(600 s).

    nvda_snapshot_300s.json   one message, seq 1
    nvda_stream_300_600s.json a JSON array, seq continuing from 2 (the same envelope)
    nvda_snapshot_600s.json   one message, seq 1 (a fresh envelope)

Run `python -B -m stonx_chart.feed.fixtures` to regenerate them. Standard library only.
"""

import json
from pathlib import Path

from stonx_chart.feed.hub import StreamHub
from stonx_chart.feed.sources import MockFeedSource
from stonx_chart.feed.wire import SOURCE_SIMULATED, Envelope

FIXTURE_SEED = 7
FIXTURE_START_MS = 1_793_975_400_000  # 2026-11-06 09:30 ET
NVDA = 2
WIRE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "wire"

_NS_PER_S = 1_000_000_000
_NS_PER_MS = 1_000_000


def build_fixtures() -> dict[str, object]:
    """Filename to JSON value, for every golden fixture."""
    start_ns = FIXTURE_START_MS * _NS_PER_MS
    hub = StreamHub(MockFeedSource(FIXTURE_SEED, start_ns), start_ns)

    def advance(second: int) -> None:
        # Exclusive boundary: `pump` is inclusive, so pumping to exactly `second` would flush the
        # first frame of a bucket now and its remaining frames next time, sending the bucket twice.
        hub.pump(start_ns + second * _NS_PER_S - 1)
        hub.flush_all()

    for second in range(1, 301):
        advance(second)
    channel, snapshot_300 = hub.subscribe(NVDA)
    envelope = Envelope(SOURCE_SIMULATED)
    snapshot_300_message = envelope.wrap(snapshot_300)

    stream = []
    for second in range(301, 601):
        advance(second)
        stream.extend(envelope.wrap(payload) for payload in channel.take(0))
    hub.unsubscribe(channel)

    snapshot_600_message = Envelope(SOURCE_SIMULATED).wrap(hub.snapshot(NVDA))
    return {
        "nvda_snapshot_300s.json": snapshot_300_message,
        "nvda_stream_300_600s.json": stream,
        "nvda_snapshot_600s.json": snapshot_600_message,
    }


def write_fixtures(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, value in build_fixtures().items():
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"
        (out_dir / name).write_bytes(text.encode("utf-8"))


def main() -> None:
    write_fixtures(WIRE_DIR)


if __name__ == "__main__":
    main()
