"""Stream hub: drives a feed source and fans its payloads out to per-connection channels.

`StreamHub` advances a `FeedSource`, folds ticks into a `SessionStore` and runs one
`OverlayDeriver` per asset. Every resulting payload goes to the `ClientChannel`s watching
that asset. A channel coalesces live updates and bounds its backlog, so a slow connection
can never stall the hub (design spec sections 6 and 9).

Threads. One pump thread calls `pump` then `flush_all` (every 50 ms in the server, the hub
has no timer of its own), and each connection's writer thread calls `subscribe`, `take`,
`resync` and `unsubscribe`. The hub lock serializes `pump`, `subscribe`, `resync`,
`unsubscribe`, `snapshot`, `flush_all`, `channel_count` and `status`, so a snapshot and the
channel registration happen between two pumps: the snapshot plus everything offered afterwards
reproduces the hub's state exactly. Each channel has its own lock and condition. The lock
order is always hub, then channel. `feed_state` and `status_message` change only under the
hub lock and may be read without it; each read is atomic, but the pair may be read between
two updates (`status` returns a consistent pair).

Payloads are shared, never copied: one dict can sit in several channels' queues, so nobody
may change a payload after offering it. `Envelope.wrap` copies. Standard library only.
"""

import threading
from collections import deque
from collections.abc import Hashable

from stonx_chart.feed.derive import OverlayDeriver
from stonx_chart.feed.records import ASSETS, TickRecord
from stonx_chart.feed.session import SessionStore
from stonx_chart.feed.sources import FeedSource
from stonx_chart.feed.wire import Payload, bar_payload, ns_to_ms, snapshot_payload, status_payload

DEFAULT_MAX_QUEUE = 2000
# One `advance` call covers at most this much sim time. A single call over a 6.5 hour backfill
# would hold every SignalFrame of it in memory at once (about 1 GB).
_MAX_CHUNK_NS = 60 * 1_000_000_000


def _slot_key(payload: Payload) -> Hashable | None:
    """The coalescing slot of a live update, or None for a message that is always delivered."""
    kind = payload["type"]
    if kind == "bar":
        return "bar"
    if kind == "series.points":
        return ("points", payload["id"])
    return None


def _time_of(payload: Payload) -> int:
    """`t` of a bar, or of a one-point `series.points` (where `points["t"]` is a one-item list)."""
    if payload["type"] == "series.points":
        return payload["points"]["t"][-1]
    return payload["t"]


class ClientChannel:
    """The pending messages of one connection, shared between the pump and the writer thread.

    Live updates (`bar` and `series.points`) are held in one slot per bar or series: a newer
    update with the same `t` replaces the held one, and one with a later `t` first moves the
    held one, which is now final, to the queue. `flush_slots` queues what is still held, in
    `t` order. Every other message goes straight to the queue, in order. A `series.points`
    payload must carry exactly one point, as `OverlayDeriver` emits them.

    When the queue grows past `max_queue` the channel drops everything it holds and sets
    `needs_snapshot`; the writer is expected to call `StreamHub.resync`. Until then further
    offers are dropped and counted too, and `take` returns an empty list at once.
    """

    def __init__(self, asset: int, max_queue: int = DEFAULT_MAX_QUEUE) -> None:
        self.asset = asset
        self._max_queue = max_queue
        self._cond = threading.Condition(threading.Lock())
        self._queue: deque[Payload] = deque()
        self._slots: dict[Hashable, Payload] = {}
        self._needs_snapshot = False
        self._discarded = 0

    @property
    def needs_snapshot(self) -> bool:
        with self._cond:
            return self._needs_snapshot

    @property
    def discarded(self) -> int:
        """Messages dropped for this connection by overflows, over its whole life."""
        with self._cond:
            return self._discarded

    def offer(self, payload: Payload) -> None:
        key = _slot_key(payload)
        with self._cond:
            if self._needs_snapshot:
                self._discarded += 1
                return
            if key is None:
                self._queue.append(payload)
            else:
                held = self._slots.get(key)
                self._slots[key] = payload
                if held is None or _time_of(held) == _time_of(payload):
                    return
                self._queue.append(held)
            self._queued()

    def flush_slots(self) -> None:
        """Queue every held update, ordered by `t`. Updates with equal `t` keep their slot order."""
        with self._cond:
            if not self._slots:
                return
            self._queue.extend(sorted(self._slots.values(), key=_time_of))
            self._slots.clear()
            self._queued()

    def take(self, timeout_s: float) -> list[Payload]:
        """Wait up to `timeout_s` for queued messages, then return and clear them.

        Returns [] on timeout, and at once when a resync becomes pending (the queue is empty then).
        """
        with self._cond:
            self._cond.wait_for(lambda: self._queue or self._needs_snapshot, timeout_s)
            taken = list(self._queue)
            self._queue.clear()
            return taken

    def reset(self) -> None:
        """Drop everything held and clear `needs_snapshot`, for a resync. `discarded` is kept."""
        with self._cond:
            self._queue.clear()
            self._slots.clear()
            self._needs_snapshot = False

    def _queued(self) -> None:
        """After the queue grew (channel lock held): overflow if it is too long, else wake the writer."""
        if len(self._queue) > self._max_queue:
            self._discarded += len(self._queue) + len(self._slots)
            self._queue.clear()
            self._slots.clear()
            self._needs_snapshot = True
        self._cond.notify_all()  # for an overflow this wakes a take() so the writer can resync


class StreamHub:
    """Pumps a feed source into a session store and 12 derivers, and serves snapshots and live payloads."""

    def __init__(self, source: FeedSource, session_start_ns: int, window_s: int = 23_400) -> None:
        self._source = source
        self.session_start_ms = ns_to_ms(session_start_ns)
        self._store = SessionStore(window_s)
        self._derivers = [
            OverlayDeriver(asset, self.session_start_ms, window_s) for asset in range(len(ASSETS))
        ]
        self._channels: list[list[ClientChannel]] = [[] for _ in ASSETS]
        self._cursor_ns = session_start_ns  # the source has been advanced to here
        self._lock = threading.Lock()
        self.feed_state = "ok"
        self.status_message: str | None = None

    # ---- pumping ------------------------------------------------------------------------

    def pump(self, until_ns: int) -> None:
        """Advance the source to `until_ns`, in chunks of at most 60 s, and route what it yields.

        If the source raises, the chunks already applied stay applied, the feed turns
        `degraded` and this call ends; the next call carries on from the same position.
        """
        with self._lock:
            while True:
                target = min(self._cursor_ns + _MAX_CHUNK_NS, until_ns)
                try:
                    records = self._source.advance(target)
                except Exception as e:
                    self._set_status("degraded", f"{type(e).__name__}: {e}")
                    return
                self._cursor_ns = max(self._cursor_ns, target)
                self._set_status("ok", None)
                self._route(records)
                if target >= until_ns:
                    return

    def flush_all(self) -> None:
        """Queue every channel's held live updates."""
        with self._lock:
            for channels in self._channels:
                for channel in channels:
                    channel.flush_slots()

    def _route(self, records: list) -> None:
        """Fold records into the store and derivers and offer the results (hub lock held)."""
        store, derivers, channels = self._store, self._derivers, self._channels
        for record in records:
            if isinstance(record, TickRecord):
                watchers = channels[record.asset_id]
                bar = store.apply_tick(record)
                if bar is not None and watchers:
                    payload = bar_payload(bar)
                    for channel in watchers:
                        channel.offer(payload)
                for op in derivers[record.asset_id].on_tick(record):
                    for channel in watchers:
                        channel.offer(op)
            else:
                for deriver, watchers in zip(derivers, channels):
                    for op in deriver.on_frame(record):
                        for channel in watchers:
                            channel.offer(op)

    def _set_status(self, feed_state: str, message: str | None) -> None:
        """Record the feed state and, if it changed, tell every channel (hub lock held)."""
        if (feed_state, message) == (self.feed_state, self.status_message):
            return
        self.feed_state, self.status_message = feed_state, message
        for channels in self._channels:
            for channel in channels:
                channel.offer(status_payload(feed_state, channel.discarded, message))

    # ---- connections --------------------------------------------------------------------

    def subscribe(self, asset: int) -> tuple[ClientChannel, dict]:
        """Register a channel for `asset`; returns it with the snapshot that matches its first message."""
        with self._lock:
            snapshot = self._snapshot(asset)
            channel = ClientChannel(asset)
            self._channels[asset].append(channel)
            return channel, snapshot

    def unsubscribe(self, channel: ClientChannel) -> None:
        with self._lock:
            try:
                self._channels[channel.asset].remove(channel)
            except ValueError:
                pass

    def resync(self, channel: ClientChannel) -> dict:
        """Empty the channel, clear `needs_snapshot` and return a fresh snapshot to send in its place."""
        with self._lock:
            channel.reset()
            return self._snapshot(channel.asset)

    def snapshot(self, asset: int) -> dict:
        with self._lock:
            return self._snapshot(asset)

    def channel_count(self) -> int:
        with self._lock:
            return sum(len(channels) for channels in self._channels)

    def status(self) -> tuple[str, str | None]:
        """`feed_state` and `status_message` read together, so the pair is never torn between two updates."""
        with self._lock:
            return self.feed_state, self.status_message

    def _snapshot(self, asset: int) -> dict:
        if not 0 <= asset < len(ASSETS):
            raise ValueError(f"unknown asset id: {asset}")
        deriver = self._derivers[asset]
        return snapshot_payload(
            asset, self.session_start_ms, self._store.bars(asset), deriver.overlays(), deriver.points()
        )
