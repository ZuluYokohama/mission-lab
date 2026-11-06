import json
import math
import random
import re

import pytest

from stonx_chart.feed.derive import OverlayDeriver
from stonx_chart.feed.records import (
    ASSETS,
    EDGES,
    FLAG_ERROR_317,
    FLAG_HALT,
    REGIME_FRAGMENTED,
    REGIME_HIGH_CURL,
    REGIME_HOLONOMY_SPIKE,
    SignalFrame,
    TickRecord,
    edge_slice,
)
from stonx_chart.feed.sources import MockFeedSource
from stonx_chart.feed.tests.factories import START_MS, START_NS, frame, last, quote

ASSET = 2  # NVDA; its touching edges are 0..3, and edge 0 is NVDA-XLK
S_NS = 1_000_000_000

EXPECTED_INITIAL = [
    {"id": "pane.lambda2", "kind": "pane", "group": "lambda2", "pane": "price",
     "title": "λ₂", "height_px": 120, "order": 1},
    {"id": "pane.curl", "kind": "pane", "group": "curl", "pane": "price",
     "title": "Star curl", "height_px": 120, "order": 2},
    {"id": "series.lambda2", "kind": "series", "group": "lambda2", "pane": "pane.lambda2",
     "style": "line", "color": "series.primary", "width": 1, "agg": "last", "precision": 3},
    {"id": "series.star_curl", "kind": "series", "group": "curl", "pane": "pane.curl",
     "style": "area", "color": "series.secondary", "width": 1, "agg": "max", "precision": 3},
    {"id": "series.bid", "kind": "series", "group": "quotes", "pane": "price",
     "style": "line", "color": "series.quote", "width": 1, "agg": "last", "precision": 2},
    {"id": "series.ask", "kind": "series", "group": "quotes", "pane": "price",
     "style": "line", "color": "series.quote", "width": 1, "agg": "last", "precision": 2},
    {"id": "series.vwap", "kind": "series", "group": "vwap", "pane": "price",
     "style": "line", "color": "series.primary", "width": 2, "agg": "last", "precision": 2},
    {"id": "level.lambda2.fragmented", "kind": "level", "group": "lambda2", "pane": "pane.lambda2",
     "value": 0.05, "color": "level.threshold", "label": "fragmented", "dash": True},
    {"id": "level.lambda2.coherent", "kind": "level", "group": "lambda2", "pane": "pane.lambda2",
     "value": 0.15, "color": "level.threshold", "label": "coherent", "dash": True},
]


def upserts(ops):
    return [op["overlay"] for op in ops if op["type"] == "overlay.upsert"]


def removed(ops):
    return [op["id"] for op in ops if op["type"] == "overlay.remove"]


def point_ops(ops, series_id):
    return [op for op in ops if op["type"] == "series.points" and op["id"] == series_id]


def feed(deriver, records):
    """Dispatch records to the deriver and return every emitted op in order."""
    ops = []
    for record in records:
        if isinstance(record, SignalFrame):
            ops.extend(deriver.on_frame(record))
        else:
            ops.extend(deriver.on_tick(record))
    return ops


def spec_by_id(deriver, overlay_id):
    return next(o for o in deriver.overlays() if o["id"] == overlay_id)


def alpha_with(edge, stalks, base=0.0):
    """72 alpha values, `base` everywhere, except `stalks` (4 values) on `edge`."""
    values = [base] * 72
    values[edge_slice(edge)] = stalks
    return tuple(values)


# ---- static specs -----------------------------------------------------------------------


def test_initial_ops_exact():
    ids = [op["overlay"]["id"] for op in OverlayDeriver(2, START_MS).initial_ops()]
    assert ids == [
        "pane.lambda2", "pane.curl", "series.lambda2", "series.star_curl", "series.bid",
        "series.ask", "series.vwap", "level.lambda2.fragmented", "level.lambda2.coherent",
    ]


def test_initial_specs_match_contract():
    ops = OverlayDeriver(2, START_MS).initial_ops()
    assert all(op["type"] == "overlay.upsert" for op in ops)
    assert upserts(ops) == EXPECTED_INITIAL


def test_static_specs_are_live_before_any_input():
    deriver = OverlayDeriver(2, START_MS)
    assert deriver.overlays() == EXPECTED_INITIAL
    assert [op["overlay"] for op in deriver.initial_ops()] == deriver.overlays()


# ---- regime and halt bands --------------------------------------------------------------


def test_regime_band_opens_and_closes():
    deriver = OverlayDeriver(ASSET, START_MS)
    assert upserts(deriver.on_frame(frame(0.0))) == []
    opened = upserts(deriver.on_frame(frame(1.0, flags=REGIME_FRAGMENTED)))
    assert opened == [{
        "id": "band.fragmented.1", "kind": "band", "group": "regime", "pane": "price",
        "start_ms": START_MS + 1000, "end_ms": None, "color": "regime.fragmented",
        "label": "FRAGMENTED",
    }]
    assert upserts(deriver.on_frame(frame(1.5, flags=REGIME_FRAGMENTED))) == []
    closed = upserts(deriver.on_frame(frame(2.5)))
    assert closed == [{**opened[0], "end_ms": START_MS + 2500}]
    assert spec_by_id(deriver, "band.fragmented.1")["end_ms"] == START_MS + 2500


def test_second_band_new_id():
    deriver = OverlayDeriver(ASSET, START_MS)
    ops = feed(deriver, [
        frame(1.0, flags=REGIME_FRAGMENTED), frame(2.0),
        frame(5.0, flags=REGIME_FRAGMENTED), frame(6.0),
    ])
    ids = [o["id"] for o in upserts(ops) if o["kind"] == "band"]
    assert ids == ["band.fragmented.1", "band.fragmented.1", "band.fragmented.2", "band.fragmented.2"]
    assert spec_by_id(deriver, "band.fragmented.2")["start_ms"] == START_MS + 5000


def test_regime_bits_are_independent():
    deriver = OverlayDeriver(ASSET, START_MS)
    ops = feed(deriver, [
        frame(1.0, flags=REGIME_FRAGMENTED | REGIME_HIGH_CURL),
        frame(2.0, flags=REGIME_HIGH_CURL | REGIME_HOLONOMY_SPIKE),
        frame(3.0),
    ])
    bands = [o for o in upserts(ops) if o["kind"] == "band"]
    assert [(b["id"], b["end_ms"] is None) for b in bands] == [
        ("band.fragmented.1", True), ("band.high_curl.1", True),
        ("band.fragmented.1", False), ("band.holonomy.1", True),
        ("band.high_curl.1", False), ("band.holonomy.1", False),
    ]
    by_id = {b["id"]: b for b in bands if b["end_ms"] is not None}
    assert by_id["band.fragmented.1"]["end_ms"] == START_MS + 2000
    assert by_id["band.high_curl.1"]["end_ms"] == START_MS + 3000
    assert by_id["band.holonomy.1"]["end_ms"] == START_MS + 3000
    assert (by_id["band.high_curl.1"]["label"], by_id["band.high_curl.1"]["color"]) == (
        "HIGH_CURL", "regime.high_curl")
    assert (by_id["band.holonomy.1"]["label"], by_id["band.holonomy.1"]["color"]) == (
        "HOLONOMY_SPIKE", "regime.holonomy")
    assert {b["group"] for b in bands} == {"regime"}


def test_halt_band_from_ticks():
    deriver = OverlayDeriver(ASSET, START_MS)
    assert feed(deriver, [quote(4, 5.0, 99.0, 100.0, flags=FLAG_HALT)]) == []
    opened = upserts(deriver.on_tick(quote(ASSET, 10.0, 99.0, 100.0, flags=FLAG_HALT)))
    assert opened == [{
        "id": "band.halt.1", "kind": "band", "group": "halt", "pane": "price",
        "start_ms": START_MS + 10_000, "end_ms": None, "color": "state.halt", "label": "HALT",
    }]
    assert upserts(deriver.on_tick(quote(ASSET, 20.0, 99.0, 100.0, flags=FLAG_HALT))) == []
    # Another asset's un-halted tick does not close this asset's band.
    assert feed(deriver, [quote(4, 25.0, 99.0, 100.0)]) == []
    closed = upserts(deriver.on_tick(quote(ASSET, 30.0, 99.0, 100.0)))
    assert closed == [{**opened[0], "end_ms": START_MS + 30_000}]


def test_halt_band_closes_on_trade_tick():
    deriver = OverlayDeriver(ASSET, START_MS)
    deriver.on_tick(quote(ASSET, 1.0, 99.0, 100.0, flags=FLAG_HALT))
    closed = upserts(deriver.on_tick(last(ASSET, 4.0, 99.5)))
    assert [(o["id"], o["end_ms"]) for o in closed] == [("band.halt.1", START_MS + 4000)]


# ---- markers ----------------------------------------------------------------------------


def test_reset_marker():
    deriver = OverlayDeriver(ASSET, START_MS)
    ops = deriver.on_tick(quote(ASSET, 5.0, 99.0, 100.0, flags=FLAG_ERROR_317))
    assert upserts(ops) == [{
        "id": "marker.reset.1", "kind": "marker", "group": "events", "pane": "price",
        "time_ms": START_MS + 5000, "position": "above", "shape": "square",
        "color": "event.reset", "text": "Book reset",
    }]
    again = upserts(deriver.on_tick(quote(ASSET, 9.0, 99.0, 100.0, flags=FLAG_ERROR_317)))
    assert [o["id"] for o in again] == ["marker.reset.2"]
    assert upserts(deriver.on_tick(quote(4, 9.5, 99.0, 100.0, flags=FLAG_ERROR_317))) == []


def baseline_alpha(rng):
    """Quiet curl noise: gaussian sigma 0.1 clipped to +-0.1, so n(e) stays at or below 0.2.

    Unclipped gaussian noise fires a surge in about 1% of frames, since n(e) is chi-distributed
    with a heavy right tail; the clip keeps the z-score under 2.5 without a spike.
    """
    return [max(-0.1, min(0.1, rng.gauss(0.0, 0.1))) for _ in range(72)]


def run_curl(asset, spikes_s, spike_edge=0, from_s=0.0, until_s=130.0, seed=11, prefix=()):
    """Feed `prefix` frames, then quiet noise every 100 ms, ×8 on `spike_edge` at each time in `spikes_s`.

    Returns (curl marker specs, deriver).
    """
    rng = random.Random(seed)
    spike_steps = {round(s * 10) for s in spikes_s}
    deriver = OverlayDeriver(asset, START_MS)
    ops = feed(deriver, prefix)
    for k in range(round(from_s * 10), round(until_s * 10) + 1):
        alpha = baseline_alpha(rng)
        if k in spike_steps:
            for i in range(*edge_slice(spike_edge).indices(72)):
                alpha[i] *= 8
        ops.extend(deriver.on_frame(frame(k / 10, alpha=tuple(alpha))))
    return [o for o in upserts(ops) if o["id"].startswith("marker.curl.")], deriver


def test_quiet_baseline_has_no_curl_markers():
    markers, _ = run_curl(ASSET, [], until_s=200.0)
    assert markers == []


def test_curl_surge_after_warmup():
    markers, deriver = run_curl(ASSET, [30.0, 90.0])
    assert markers == [{
        "id": "marker.curl.1", "kind": "marker", "group": "events", "pane": "price",
        "time_ms": START_MS + 90_000, "position": "below", "shape": "circle",
        "color": "event.curl_surge", "text": "Curl surge NVDA–XLK",
    }]
    assert spec_by_id(deriver, "marker.curl.1") == markers[0]


def test_curl_warmup_counts_from_first_frame():
    # The deriver first sees a frame at 100 s: a spike 30 s later is inside the warm-up,
    # a spike 70 s later is not.
    markers, _ = run_curl(ASSET, [130.0, 170.0], from_s=100.0, until_s=200.0)
    assert [m["time_ms"] for m in markers] == [START_MS + 170_000]


def test_curl_surge_cooldown():
    markers, _ = run_curl(ASSET, [90.0, 100.0])
    assert [m["time_ms"] for m in markers] == [START_MS + 90_000]
    markers, _ = run_curl(ASSET, [90.0, 125.0])
    assert [m["id"] for m in markers] == ["marker.curl.1", "marker.curl.2"]
    assert [m["time_ms"] for m in markers] == [START_MS + 90_000, START_MS + 125_000]


def test_curl_ignores_other_edges():
    # Edge 10 is (XLK, MSFT): it does not touch NVDA but does touch XLK (0) and MSFT (7).
    assert 2 not in EDGES[10]
    markers, _ = run_curl(ASSET, [90.0], spike_edge=10)
    assert markers == []
    markers, _ = run_curl(0, [90.0], spike_edge=10)
    assert [m["text"] for m in markers] == ["Curl surge XLK–MSFT"]


def test_curl_never_fires_on_zero_variance():
    # A constant, non-zero n(e) has sigma == 0 up to float noise in the rolling sums.
    deriver = OverlayDeriver(ASSET, START_MS)
    steady = (0.3, 0.1, 0.2, 0.05)
    ops = []
    for k in range(900):
        ops.extend(deriver.on_frame(frame(k / 10, alpha=alpha_with(0, steady))))
    bump = tuple(x * 8 for x in steady)
    ops.extend(deriver.on_frame(frame(90.0, alpha=alpha_with(0, bump))))
    assert [o for o in upserts(ops) if o["kind"] == "marker"] == []


# ---- series points ----------------------------------------------------------------------


def test_points_reduce_by_agg():
    deriver = OverlayDeriver(ASSET, START_MS)
    ops = feed(deriver, [
        frame(0.2, lam=0.3, alpha=alpha_with(0, (2.0, 0.0, 0.0, 0.0))),
        frame(0.7, lam=0.1, alpha=alpha_with(0, (1.0, 0.0, 0.0, 0.0))),
    ])
    lam = point_ops(ops, "series.lambda2")
    assert [(p["t"], p["v"]) for p in lam] == [([START_MS], [0.3]), ([START_MS], [0.1])]
    star = point_ops(ops, "series.star_curl")
    assert [(p["t"], p["v"]) for p in star] == [([START_MS], [2.0]), ([START_MS], [2.0])]
    points = deriver.points()
    assert points["series.lambda2"] == {"t": [START_MS], "v": [0.1]}
    assert points["series.star_curl"] == {"t": [START_MS], "v": [2.0]}


def test_star_curl_covers_touching_edges_only():
    deriver = OverlayDeriver(ASSET, START_MS)
    alpha = list(alpha_with(0, (3.0, 0.0, 0.0, 0.0)))
    alpha[edge_slice(1)] = (0.0, 4.0, 0.0, 0.0)
    alpha[edge_slice(10)] = (100.0, 0.0, 0.0, 0.0)  # edge 10 does not touch NVDA
    ops = deriver.on_frame(frame(0.0, alpha=tuple(alpha)))
    assert point_ops(ops, "series.star_curl")[0]["v"] == [pytest.approx(5.0)]


def test_bid_ask_ignore_trade_ticks_and_stay_finite():
    deriver = OverlayDeriver(ASSET, START_MS)
    ops = feed(deriver, [
        quote(ASSET, 0.1, 99.0, 100.0),
        last(ASSET, 0.2, 99.5),
        quote(ASSET, 1.2, 99.5, 100.5),
        last(ASSET, 1.4, 100.0),
    ])
    bid = point_ops(ops, "series.bid")
    ask = point_ops(ops, "series.ask")
    assert [(p["t"], p["v"]) for p in bid] == [([START_MS], [99.0]), ([START_MS + 1000], [99.5])]
    assert [(p["t"], p["v"]) for p in ask] == [([START_MS], [100.0]), ([START_MS + 1000], [100.5])]
    for op in ops:
        if op["type"] == "series.points":
            assert all(math.isfinite(v) for v in op["v"])
    json.dumps(ops, allow_nan=False)
    assert deriver.points()["series.bid"] == {"t": [START_MS, START_MS + 1000], "v": [99.0, 99.5]}


def test_non_finite_values_are_dropped():
    deriver = OverlayDeriver(ASSET, START_MS)
    ops = deriver.on_tick(quote(ASSET, 0.1, 99.0, math.nan))
    assert [(p["id"], p["v"]) for p in point_ops(ops, "series.bid")] == [("series.bid", [99.0])]
    assert point_ops(ops, "series.ask") == []
    ops = deriver.on_frame(frame(0.2, lam=math.nan, alpha=alpha_with(0, (math.nan, 0.0, 0.0, 0.0))))
    assert [op for op in ops if op["type"] == "series.points"] == []
    json.dumps(deriver.points(), allow_nan=False)
    # A NaN sample must not poison later points ...
    ops = deriver.on_frame(frame(0.3, lam=0.2, alpha=alpha_with(0, (0.3, 0.0, 0.0, 0.0))))
    assert point_ops(ops, "series.star_curl")[0]["v"] == [pytest.approx(0.3)]
    # ... or the surge statistics of the edge it came in on.
    nan_frame = frame(-0.1, alpha=alpha_with(0, (math.nan, 0.0, 0.0, 0.0)))
    markers, _ = run_curl(ASSET, [90.0], prefix=[nan_frame])
    assert [m["time_ms"] for m in markers] == [START_MS + 90_000]


def test_other_asset_ticks_emit_nothing():
    deriver = OverlayDeriver(ASSET, START_MS)
    assert feed(deriver, [quote(4, 1.0, 99.0, 100.0, flags=FLAG_HALT | FLAG_ERROR_317), last(4, 2.0, 99.0)]) == []
    assert deriver.points()["series.bid"] == {"t": [], "v": []}
    assert deriver.points()["series.vwap"] == {"t": [], "v": []}


def test_vwap():
    deriver = OverlayDeriver(ASSET, START_MS)
    ops = feed(deriver, [last(ASSET, 0.5, 100.0, size=1.0), last(ASSET, 0.8, 102.0, size=3.0)])
    vwap = point_ops(ops, "series.vwap")
    assert [(p["t"], p["v"]) for p in vwap] == [([START_MS], [100.0]), ([START_MS], [pytest.approx(101.5)])]
    assert deriver.points()["series.vwap"]["v"] == [pytest.approx(101.5)]
    # Quotes do not move the VWAP.
    assert point_ops(deriver.on_tick(quote(ASSET, 1.0, 50.0, 51.0)), "series.vwap") == []


def test_vwap_covers_trades_since_session_start_with_size():
    deriver = OverlayDeriver(ASSET, START_MS)
    assert point_ops(deriver.on_tick(last(ASSET, -5.0, 10.0, size=100.0)), "series.vwap") == []
    assert point_ops(deriver.on_tick(last(ASSET, 1.0, 100.0, size=0.0)), "series.vwap") == []
    ops = deriver.on_tick(last(ASSET, 2.0, 100.0, size=2.0))
    assert point_ops(ops, "series.vwap")[0]["v"] == [pytest.approx(100.0)]


def test_overlays_and_points_snapshot():
    deriver = OverlayDeriver(ASSET, START_MS)
    feed(deriver, [frame(k / 2, lam=0.1 + k / 100, flags=REGIME_FRAGMENTED if k >= 2 else 0) for k in range(11)])
    band = spec_by_id(deriver, "band.fragmented.1")
    assert band["end_ms"] is None
    assert band["start_ms"] == START_MS + 1000
    # Statics first, in initial order, then events in first-upsert order.
    assert [o["id"] for o in deriver.overlays()] == [o["id"] for o in EXPECTED_INITIAL] + ["band.fragmented.1"]
    lam = deriver.points()["series.lambda2"]
    assert lam["t"] == [START_MS + 1000 * i for i in range(6)]
    assert lam["v"] == [pytest.approx(0.1 + min(2 * i + 1, 10) / 100) for i in range(6)]
    assert set(deriver.points()) == {
        "series.lambda2", "series.star_curl", "series.bid", "series.ask", "series.vwap"}


def test_overlays_and_points_return_copies():
    deriver = OverlayDeriver(ASSET, START_MS)
    ops = feed(deriver, [frame(0.0, flags=REGIME_FRAGMENTED), quote(ASSET, 0.1, 99.0, 100.0)])
    snapshot = deriver.overlays()
    snapshot[-1]["end_ms"] = 1
    snapshot[0]["title"] = "mutated"
    snapshot.pop()
    for op in ops:
        if op["type"] == "overlay.upsert":
            op["overlay"]["label"] = "mutated"
    points = deriver.points()
    points["series.bid"]["v"].append(1.0)
    points["series.bid"]["t"].clear()
    assert spec_by_id(deriver, "band.fragmented.1")["end_ms"] is None
    assert spec_by_id(deriver, "band.fragmented.1")["label"] == "FRAGMENTED"
    assert spec_by_id(deriver, "pane.lambda2")["title"] == "λ₂"
    assert deriver.points()["series.bid"] == {"t": [START_MS], "v": [99.0]}
    # initial_ops hands out fresh dicts as well.
    first = deriver.initial_ops()
    first[0]["overlay"]["title"] = "mutated"
    assert deriver.initial_ops()[0]["overlay"]["title"] == "λ₂"


# ---- eviction ---------------------------------------------------------------------------


def test_eviction_removes_old_overlays():
    deriver = OverlayDeriver(ASSET, START_MS, window_s=60)
    feed(deriver, [
        frame(5.0, flags=REGIME_FRAGMENTED), frame(10.0),  # closed band, ends at 10 s
        frame(12.0, flags=REGIME_HIGH_CURL),  # band that stays open
    ])
    # cutoff = 70 s - 60 s = 10 s: a band ending exactly at the cutoff stays.
    assert removed(deriver.on_frame(frame(70.0, flags=REGIME_HIGH_CURL))) == []
    ops = deriver.on_frame(frame(70.1, flags=REGIME_HIGH_CURL))
    assert removed(ops) == ["band.fragmented.1"]
    assert "band.fragmented.1" not in [o["id"] for o in deriver.overlays()]
    # Eviction is not repeated, and the open band and static specs stay.
    assert removed(deriver.on_frame(frame(80.0, flags=REGIME_HIGH_CURL))) == []
    ids = [o["id"] for o in deriver.overlays()]
    assert "band.high_curl.1" in ids
    assert ids[: len(EXPECTED_INITIAL)] == [o["id"] for o in EXPECTED_INITIAL]
    # The removed id is not reused by the next band of its kind.
    ops = deriver.on_frame(frame(81.0, flags=REGIME_HIGH_CURL | REGIME_FRAGMENTED))
    assert [o["id"] for o in upserts(ops)] == ["band.fragmented.2"]


def test_eviction_removes_old_markers_and_points():
    deriver = OverlayDeriver(ASSET, START_MS, window_s=60)
    feed(deriver, [
        quote(ASSET, 5.0, 99.0, 100.0, flags=FLAG_ERROR_317),
        quote(ASSET, 40.0, 99.0, 100.0, flags=FLAG_ERROR_317),
        frame(5.0), frame(40.0),
    ])
    ops = deriver.on_frame(frame(80.0))  # cutoff 20 s
    assert removed(ops) == ["marker.reset.1"]
    assert "marker.reset.1" not in [o["id"] for o in deriver.overlays()]
    assert "marker.reset.2" in [o["id"] for o in deriver.overlays()]
    points = deriver.points()
    assert points["series.lambda2"]["t"] == [START_MS + 40_000, START_MS + 80_000]
    assert points["series.bid"]["t"] == [START_MS + 40_000]
    # Dropping stored points sends no wire op.
    assert all(op["type"] in ("overlay.remove", "series.points") for op in ops)


def test_default_window_keeps_a_full_session():
    deriver = OverlayDeriver(ASSET, START_MS)
    feed(deriver, [frame(5.0, flags=REGIME_FRAGMENTED), frame(10.0), frame(23_000.0)])
    assert "band.fragmented.1" in [o["id"] for o in deriver.overlays()]
    assert len(deriver.points()["series.lambda2"]["t"]) == 3


# ---- whole-feed properties --------------------------------------------------------------


@pytest.fixture(scope="module")
def mock_run():
    """600 s of MockFeedSource(7) through one deriver per asset: (all ops, final overlays)."""
    derivers = [OverlayDeriver(a, START_MS) for a in range(len(ASSETS))]
    ops = [op for d in derivers for op in d.initial_ops()]
    source = MockFeedSource(7, START_NS)
    for chunk in range(1, 11):
        for record in source.advance(START_NS + chunk * 60 * S_NS):
            for d in derivers:
                if isinstance(record, TickRecord):
                    ops.extend(d.on_tick(record))
                else:
                    ops.extend(d.on_frame(record))
    return ops, [o for d in derivers for o in d.overlays()]


def test_no_buy_sell_text(mock_run):
    ops, final = mock_run
    emitted = upserts(ops) + final
    texts = [o[key] for o in emitted for key in ("label", "text", "title") if key in o]
    assert {"HALT", "Book reset"} <= set(texts)
    assert not [t for t in texts if re.search(r"buy|sell", t, re.IGNORECASE)]


def test_mock_run_ops_are_wire_safe(mock_run):
    ops, final = mock_run
    json.dumps(ops, allow_nan=False)
    json.dumps(final, allow_nan=False)
    tokens = {"regime.fragmented", "regime.high_curl", "regime.holonomy", "state.halt", "event.reset",
              "event.curl_surge", "series.primary", "series.secondary", "series.quote", "level.threshold",
              "neutral"}
    colors = {o["color"] for o in upserts(ops) if "color" in o}  # panes have no color
    assert {"regime.fragmented", "state.halt", "event.reset", "series.quote"} <= colors <= tokens
