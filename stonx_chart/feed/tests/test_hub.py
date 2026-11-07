import json
import threading
import time

import pytest

from stonx_chart.feed.derive import OverlayDeriver
from stonx_chart.feed.hub import ClientChannel, StreamHub
from stonx_chart.feed.records import TickRecord
from stonx_chart.feed.session import Bar, SessionStore
from stonx_chart.feed.sources import MockFeedSource
from stonx_chart.feed.tests.factories import START_MS, START_NS
from stonx_chart.feed.wire import bar_payload

S = 1_000_000_000
NVDA = 2
AMD = 4
MAX_QUEUE = 2000


def bar_msg(t: int, c: float = 1.0) -> dict:
    return {"type": "bar", "t": t, "o": c, "h": c, "l": c, "c": c, "vol": 1.0}


def points_msg(series_id: str, t: int, v: float = 1.0) -> dict:
    return {"type": "series.points", "id": series_id, "points": {"t": [t], "v": [v]}}


def upsert_msg(overlay_id: str, n: int = 0) -> dict:
    return {"type": "overlay.upsert", "overlay": {"id": overlay_id, "n": n}}


def new_hub(**kwargs) -> StreamHub:
    return StreamHub(MockFeedSource(7, START_NS), START_NS, **kwargs)


def reference(asset: int, until_ns: int) -> tuple[SessionStore, OverlayDeriver]:
    """A store and a deriver fed straight from a fresh source in a single advance."""
    store = SessionStore()
    deriver = OverlayDeriver(asset, START_MS)
    for record in MockFeedSource(7, START_NS).advance(until_ns):
        if isinstance(record, TickRecord):
            store.apply_tick(record)
            deriver.on_tick(record)
        else:
            deriver.on_frame(record)
    return store, deriver


def columns(bars: list[Bar]) -> dict:
    return {
        "t": [b.t_ms for b in bars], "o": [b.o for b in bars], "h": [b.h for b in bars],
        "l": [b.l for b in bars], "c": [b.c for b in bars], "vol": [b.vol for b in bars],
    }


def state_of(snapshot: dict) -> tuple[dict, dict, dict]:
    """What a client holds after a snapshot: bars by t, overlays by id, points by series and t."""
    b = snapshot["bars"]
    rows = zip(b["t"], b["o"], b["h"], b["l"], b["c"], b["vol"])
    bars = {t: (o, h, lo, c, v) for t, o, h, lo, c, v in rows}
    overlays = {o["id"]: o for o in snapshot["overlays"]}
    points = {sid: dict(zip(p["t"], p["v"])) for sid, p in snapshot["points"].items()}
    return bars, overlays, points


def apply_messages(state: tuple[dict, dict, dict], messages: list[dict]) -> None:
    bars, overlays, points = state
    for m in messages:
        if m["type"] == "bar":
            bars[m["t"]] = (m["o"], m["h"], m["l"], m["c"], m["vol"])
        elif m["type"] == "overlay.upsert":
            overlays[m["overlay"]["id"]] = m["overlay"]
        elif m["type"] == "overlay.remove":
            overlays.pop(m["id"], None)
        elif m["type"] == "series.points":
            points[m["id"]].update(zip(m["points"]["t"], m["points"]["v"]))


class SpySource:
    """Wraps a real source, records every advance call and raises on chosen (1-based) calls.

    A failing call does not advance the inner source, like a source that fails before it
    hands anything over.
    """

    drop_count = 0

    def __init__(self, inner, failures: dict[int, Exception] | None = None) -> None:
        self.inner = inner
        self.calls: list[int] = []
        self.failures = failures or {}

    def advance(self, until_ns: int) -> list:
        self.calls.append(until_ns)
        failure = self.failures.get(len(self.calls))
        if failure is not None:
            raise failure
        return self.inner.advance(until_ns)


def statuses(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m["type"] == "status"]


# ---- hub routing ------------------------------------------------------------------------


def test_snapshot_then_only_own_asset():
    hub = new_hub()
    hub.pump(START_NS + 60 * S)
    channel, snap = hub.subscribe(NVDA)
    assert snap["type"] == "snapshot" and snap["asset"] == "NVDA"
    assert snap["bars"] == columns(reference(NVDA, START_NS + 60 * S)[0].bars(NVDA))

    hub.pump(START_NS + 61 * S)
    hub.flush_all()
    messages = channel.take(0)
    assert messages

    ref_store, ref_deriver = reference(NVDA, START_NS + 61 * S)
    ref_bars = {b.t_ms: bar_payload(b) for b in ref_store.bars(NVDA)}
    ref_points = ref_deriver.points()
    bars = [m for m in messages if m["type"] == "bar"]
    points = [m for m in messages if m["type"] == "series.points"]
    assert bars and points
    for m in bars:
        assert m == ref_bars[m["t"]]
    for m in points:
        series = dict(zip(ref_points[m["id"]]["t"], ref_points[m["id"]]["v"]))
        assert series[m["points"]["t"][0]] == m["points"]["v"][0]


def test_frames_reach_every_channel_but_ticks_only_their_own_asset():
    hub = new_hub()
    nvda, _ = hub.subscribe(NVDA)
    amd, _ = hub.subscribe(AMD)
    hub.pump(START_NS + 3 * S)
    hub.flush_all()
    for asset, channel in ((NVDA, nvda), (AMD, amd)):
        messages = channel.take(0)
        assert any(m["type"] == "series.points" and m["id"] == "series.lambda2" for m in messages)
        ref_store, _ = reference(asset, START_NS + 3 * S)
        ref_bars = {b.t_ms: bar_payload(b) for b in ref_store.bars(asset)}
        bars = [m for m in messages if m["type"] == "bar"]
        assert bars and all(m == ref_bars[m["t"]] for m in bars)


def test_snapshot_plus_stream_replays_to_the_current_snapshot():
    hub = new_hub()
    hub.pump(START_NS + 100 * S)
    channel, snap = hub.subscribe(NVDA)
    state = state_of(snap)
    for s in range(101, 421):
        hub.pump(START_NS + s * S)
        if s % 3 == 0:  # slots keep several seconds between flushes
            hub.flush_all()
            apply_messages(state, channel.take(0))
    hub.flush_all()
    apply_messages(state, channel.take(0))
    assert state == state_of(hub.snapshot(NVDA))
    dynamic = [i for i in state[1] if i.startswith(("marker.", "band."))]
    assert dynamic, "the run should produce dynamic overlays"


def test_pump_advances_source_in_chunks_of_at_most_60_s():
    spy = SpySource(MockFeedSource(7, START_NS))
    hub = StreamHub(spy, START_NS)
    hub.pump(START_NS + 150 * S)
    assert spy.calls == [START_NS + 60 * S, START_NS + 120 * S, START_NS + 150 * S]
    hub.pump(START_NS + 160 * S)  # continues from the 150 s cursor
    assert spy.calls[3:] == [START_NS + 160 * S]
    hub.pump(START_NS + 280 * S)
    assert spy.calls[4:] == [START_NS + 220 * S, START_NS + 280 * S]

    exact = SpySource(MockFeedSource(7, START_NS))
    StreamHub(exact, START_NS).pump(START_NS + 60 * S)
    assert exact.calls == [START_NS + 60 * S]


def test_chunking_is_invisible():
    one_call = new_hub()
    one_call.pump(START_NS + 200 * S)
    many_calls = new_hub()
    for s in range(1, 201):
        many_calls.pump(START_NS + s * S)

    snap = one_call.snapshot(NVDA)
    ref_store, ref_deriver = reference(NVDA, START_NS + 200 * S)
    assert snap["bars"] == columns(ref_store.bars(NVDA))
    assert snap["overlays"] == ref_deriver.overlays()
    assert snap["points"] == ref_deriver.points()
    assert json.dumps(many_calls.snapshot(NVDA)) == json.dumps(snap)


def test_session_start_ms_is_floored_ns():
    hub = StreamHub(MockFeedSource(7, START_NS), START_NS + 999_999)
    assert hub.session_start_ms == START_MS
    assert hub.snapshot(NVDA)["session_start_ms"] == START_MS


def test_snapshot_of_an_empty_hub_still_lists_static_overlays():
    snap = new_hub().snapshot(NVDA)
    assert snap["bars"]["t"] == []
    assert {"pane.lambda2", "series.bid", "level.lambda2.coherent"} <= {o["id"] for o in snap["overlays"]}
    assert set(snap["points"]) >= {"series.lambda2", "series.vwap"}


@pytest.mark.parametrize("bad", [-1, 12])
def test_unknown_asset_rejected(bad):
    hub = new_hub()
    with pytest.raises(ValueError):
        hub.subscribe(bad)
    with pytest.raises(ValueError):
        hub.snapshot(bad)
    assert hub.channel_count() == 0


# ---- subscribe, resync, unsubscribe -----------------------------------------------------------


def test_resync_returns_current_state():
    hub = new_hub()
    hub.pump(START_NS + 60 * S)
    channel, first = hub.subscribe(NVDA)
    hub.pump(START_NS + 90 * S)
    hub.flush_all()

    snap = hub.resync(channel)
    latest = reference(NVDA, START_NS + 90 * S)[0].bars(NVDA)[-1]
    assert {k: v[-1] for k, v in snap["bars"].items()} == {
        "t": latest.t_ms, "o": latest.o, "h": latest.h, "l": latest.l, "c": latest.c, "vol": latest.vol,
    }
    assert snap["bars"]["t"][-1] > first["bars"]["t"][-1]
    assert channel.take(0) == [] and not channel.needs_snapshot

    hub.pump(START_NS + 91 * S)
    hub.flush_all()
    assert channel.take(0)


def test_overflow_then_resync_resumes_delivery():
    hub = new_hub()
    hub.pump(START_NS + 60 * S)
    channel, _ = hub.subscribe(NVDA)
    for n in range(MAX_QUEUE + 1):
        channel.offer(upsert_msg("x", n))
    assert channel.needs_snapshot and channel.discarded == MAX_QUEUE + 1

    hub.pump(START_NS + 62 * S)  # dropped: a resync is pending
    hub.flush_all()
    assert channel.take(0) == []
    dropped = channel.discarded
    assert dropped > MAX_QUEUE + 1

    snap = hub.resync(channel)
    assert not channel.needs_snapshot and channel.discarded == dropped
    assert snap["bars"] == columns(reference(NVDA, START_NS + 62 * S)[0].bars(NVDA))

    hub.pump(START_NS + 63 * S)
    hub.flush_all()
    assert channel.take(0)


def test_unsubscribe_stops_delivery():
    hub = new_hub()
    gone, _ = hub.subscribe(NVDA)
    kept, _ = hub.subscribe(NVDA)
    assert hub.channel_count() == 2
    hub.unsubscribe(gone)
    assert hub.channel_count() == 1
    hub.unsubscribe(gone)  # unsubscribing twice is harmless
    assert hub.channel_count() == 1

    hub.pump(START_NS + 3 * S)
    hub.flush_all()
    assert gone.take(0) == []
    assert kept.take(0)
    hub.unsubscribe(kept)
    assert hub.channel_count() == 0


# ---- source errors --------------------------------------------------------------------------


def test_source_error_degrades_and_recovers():
    spy = SpySource(MockFeedSource(7, START_NS), {1: RuntimeError("boom")})
    hub = StreamHub(spy, START_NS)
    assert (hub.feed_state, hub.status_message) == ("ok", None)
    channel, _ = hub.subscribe(NVDA)

    hub.pump(START_NS + 2 * S)
    assert (hub.feed_state, hub.status_message) == ("degraded", "RuntimeError: boom")
    assert statuses(channel.take(0)) == [
        {"type": "status", "feed_state": "degraded", "drop_count": 0, "message": "RuntimeError: boom"},
    ]

    hub.pump(START_NS + 2 * S)
    hub.flush_all()
    assert (hub.feed_state, hub.status_message) == ("ok", None)
    messages = channel.take(0)
    assert statuses(messages) == [{"type": "status", "feed_state": "ok", "drop_count": 0}]
    assert messages.index(statuses(messages)[0]) == 0
    assert any(m["type"] == "bar" for m in messages)


def test_status_reads_state_and_message_under_the_hub_lock():
    entered, release = threading.Event(), threading.Event()

    class BlockingSource:
        drop_count = 0

        def advance(self, until_ns: int) -> list:
            entered.set()
            release.wait(5.0)
            raise RuntimeError("boom")

    hub = StreamHub(BlockingSource(), START_NS)
    assert hub.status() == ("ok", None)
    pumper = threading.Thread(target=hub.pump, args=(START_NS + S,))
    pumper.start()
    assert entered.wait(5.0)
    result: list[tuple[str, str | None]] = []
    reader = threading.Thread(target=lambda: result.append(hub.status()))
    reader.start()
    reader.join(0.1)
    assert reader.is_alive()  # an unlocked read would have returned the stale pair at once
    release.set()
    for thread in (reader, pumper):
        thread.join(5.0)
        assert not thread.is_alive()
    assert result == [("degraded", "RuntimeError: boom")]


def test_status_is_offered_only_when_it_changes():
    spy = SpySource(
        MockFeedSource(7, START_NS),
        {1: RuntimeError("boom"), 2: RuntimeError("boom"), 3: ValueError("other")},
    )
    hub = StreamHub(spy, START_NS)
    channel, _ = hub.subscribe(NVDA)
    for _ in range(5):
        hub.pump(START_NS + 2 * S)
    assert [(m["feed_state"], m.get("message")) for m in statuses(channel.take(0))] == [
        ("degraded", "RuntimeError: boom"),
        ("degraded", "ValueError: other"),
        ("ok", None),
    ]


def test_failed_chunk_keeps_earlier_chunks_and_next_pump_retries():
    spy = SpySource(MockFeedSource(7, START_NS), {2: RuntimeError("boom")})
    hub = StreamHub(spy, START_NS)
    hub.pump(START_NS + 150 * S)  # the chunk to 60 s is applied, the one to 120 s raises
    assert hub.feed_state == "degraded"
    assert hub.snapshot(NVDA)["bars"] == columns(reference(NVDA, START_NS + 60 * S)[0].bars(NVDA))

    hub.pump(START_NS + 150 * S)  # retries from where the source stands
    assert hub.feed_state == "ok" and hub.status_message is None
    assert spy.calls == [START_NS + 60 * S, START_NS + 120 * S, START_NS + 120 * S, START_NS + 150 * S]
    ref_store, ref_deriver = reference(NVDA, START_NS + 150 * S)
    snap = hub.snapshot(NVDA)
    assert snap["bars"] == columns(ref_store.bars(NVDA))
    assert snap["points"] == ref_deriver.points()


def test_status_reports_each_channels_discarded_count():
    spy = SpySource(MockFeedSource(7, START_NS), {2: RuntimeError("boom")})
    hub = StreamHub(spy, START_NS)
    hub.pump(START_NS + 60 * S)
    overflowed, _ = hub.subscribe(NVDA)
    clean, _ = hub.subscribe(AMD)
    for n in range(MAX_QUEUE + 1):
        overflowed.offer(upsert_msg("x", n))
    hub.resync(overflowed)

    hub.pump(START_NS + 61 * S)
    assert statuses(overflowed.take(0))[0]["drop_count"] == MAX_QUEUE + 1
    assert statuses(clean.take(0))[0]["drop_count"] == 0


# ---- channel coalescing ---------------------------------------------------------------------


def test_coalesce_same_t_replaced():
    channel = ClientChannel(NVDA)
    channel.offer(bar_msg(1, c=1.0))
    channel.offer(bar_msg(1, c=2.0))
    channel.flush_slots()
    assert channel.take(0) == [bar_msg(1, c=2.0)]


def test_coalesce_new_t_pushes_previous():
    channel = ClientChannel(NVDA)
    channel.offer(bar_msg(1))
    channel.offer(bar_msg(2))
    assert channel.take(0) == [bar_msg(1)]  # the older bar is final, so it is queued at once
    channel.flush_slots()
    assert channel.take(0) == [bar_msg(2)]

    channel.offer(bar_msg(3))
    channel.offer(bar_msg(4))
    channel.flush_slots()
    assert channel.take(0) == [bar_msg(3), bar_msg(4)]


def test_coalesce_points_per_series():
    channel = ClientChannel(NVDA)
    channel.offer(points_msg("series.bid", 1, 1.0))
    channel.offer(points_msg("series.ask", 1, 5.0))
    channel.offer(points_msg("series.bid", 1, 2.0))
    channel.offer(points_msg("series.ask", 2, 6.0))
    channel.flush_slots()
    assert channel.take(0) == [
        points_msg("series.ask", 1, 5.0),
        points_msg("series.bid", 1, 2.0),
        points_msg("series.ask", 2, 6.0),
    ]


def test_flush_orders_slots_by_t():
    channel = ClientChannel(NVDA)
    channel.offer(points_msg("series.lambda2", 2))
    channel.offer(bar_msg(1))
    channel.flush_slots()
    assert channel.take(0) == [bar_msg(1), points_msg("series.lambda2", 2)]


def test_flush_with_nothing_held_is_a_no_op():
    channel = ClientChannel(NVDA)
    channel.flush_slots()
    assert channel.take(0) == []


def test_overlay_ops_not_coalesced():
    channel = ClientChannel(NVDA)
    removal = {"type": "overlay.remove", "id": "a"}
    messages = [upsert_msg("band.halt.1", 1), upsert_msg("band.halt.1", 2), removal]
    for message in messages:
        channel.offer(message)
    assert channel.take(0) == messages  # no flush needed


def test_take_returns_and_clears():
    channel = ClientChannel(NVDA)
    channel.offer(upsert_msg("a"))
    assert channel.take(0) == [upsert_msg("a")]
    assert channel.take(0) == []


# ---- channel overflow and blocking ----------------------------------------------------------


def test_overflow_requests_resync():
    channel = ClientChannel(NVDA, max_queue=10)
    for n in range(10):
        channel.offer(upsert_msg("a", n))
    assert not channel.needs_snapshot and channel.discarded == 0
    channel.offer(upsert_msg("a", 10))
    assert channel.needs_snapshot
    assert channel.discarded == 11
    assert channel.take(0) == []


def test_overflow_counts_held_slots_too():
    channel = ClientChannel(NVDA, max_queue=2)
    channel.offer(bar_msg(1))  # held in its slot
    for n in range(3):
        channel.offer(upsert_msg("a", n))
    assert channel.needs_snapshot and channel.discarded == 4
    channel.flush_slots()
    assert channel.take(0) == []


def test_overflow_by_flush():
    channel = ClientChannel(NVDA, max_queue=2)
    for series_id in ("series.a", "series.b", "series.c"):
        channel.offer(points_msg(series_id, 1))
    assert not channel.needs_snapshot
    channel.flush_slots()
    assert channel.needs_snapshot and channel.discarded == 3
    assert channel.take(0) == []


def test_offers_while_resync_is_pending_are_dropped_and_counted():
    channel = ClientChannel(NVDA, max_queue=2)
    for n in range(3):
        channel.offer(upsert_msg("a", n))
    assert channel.discarded == 3
    channel.offer(upsert_msg("b"))
    channel.offer(bar_msg(1))
    assert channel.discarded == 5
    channel.flush_slots()
    assert channel.take(0) == [] and channel.needs_snapshot


def test_take_waits_for_a_message():
    channel = ClientChannel(NVDA)
    timer = threading.Timer(0.05, channel.offer, [upsert_msg("a")])
    timer.start()
    started = time.perf_counter()
    assert channel.take(5.0) == [upsert_msg("a")]
    assert time.perf_counter() - started < 2.0
    timer.join()


def test_take_times_out_empty():
    assert ClientChannel(NVDA).take(0.01) == []


def test_overflow_wakes_a_blocked_take():
    channel = ClientChannel(NVDA, max_queue=2)
    waiting = threading.Event()
    result: list = []

    def writer():
        waiting.set()
        started = time.perf_counter()
        result.append((channel.take(10.0), time.perf_counter() - started))

    thread = threading.Thread(target=writer)
    thread.start()
    waiting.wait()
    time.sleep(0.05)  # let the writer block inside take
    for n in range(3):
        channel.offer(upsert_msg("a", n))
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    messages, elapsed = result[0]
    assert messages == [] and elapsed < 5.0
    assert channel.needs_snapshot


# ---- threads ------------------------------------------------------------------------------


def test_concurrent_subscribers_end_up_consistent_with_the_hub():
    hub = new_hub()
    hub.pump(START_NS + 90 * S)
    done = threading.Event()
    errors: list[BaseException] = []
    results: dict[str, tuple[int, tuple]] = {}

    def pumper():
        try:
            for s in range(91, 300):
                hub.pump(START_NS + s * S)
                hub.flush_all()
                time.sleep(0.0005)
        except BaseException as exc:  # reported on the main thread
            errors.append(exc)
        finally:
            done.set()

    def consumer(name: str, asset: int, delay_s: float):
        try:
            time.sleep(delay_s)
            channel, snap = hub.subscribe(asset)
            state = state_of(snap)
            while True:
                finished = done.is_set()
                messages = channel.take(0.01)
                if channel.needs_snapshot:
                    state = state_of(hub.resync(channel))
                    continue
                apply_messages(state, messages)
                if finished and not messages:
                    break
            hub.unsubscribe(channel)
            results[name] = (asset, state)
        except BaseException as exc:
            errors.append(exc)

    consumers = [
        threading.Thread(target=consumer, args=("early", NVDA, 0.0)),
        threading.Thread(target=consumer, args=("middle", AMD, 0.05)),
        threading.Thread(target=consumer, args=("late", NVDA, 0.15)),
    ]
    for thread in consumers:
        thread.start()
    pump_thread = threading.Thread(target=pumper)
    pump_thread.start()
    for thread in (*consumers, pump_thread):
        thread.join(timeout=60)
        assert not thread.is_alive()
    assert errors == []
    assert sorted(results) == ["early", "late", "middle"]
    for asset, state in results.values():
        assert state == state_of(hub.snapshot(asset))
    assert hub.channel_count() == 0
