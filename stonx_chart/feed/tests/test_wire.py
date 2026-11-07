import json
import math

import pytest

from stonx_chart.feed.records import ASSETS
from stonx_chart.feed.session import Bar
from stonx_chart.feed.wire import (
    SCHEMA_VERSION,
    SOURCE_SIMULATED,
    Envelope,
    bar_payload,
    encode_sse,
    heartbeat_payload,
    ns_to_ms,
    snapshot_payload,
    status_payload,
)


def test_constants():
    assert SCHEMA_VERSION == 1
    assert SOURCE_SIMULATED == "SIMULATED_DEMO"


def test_envelope_order_and_seq():
    e = Envelope("SIMULATED_DEMO")
    m1 = e.wrap(heartbeat_payload(5))
    m2 = e.wrap(heartbeat_payload(6))
    assert list(m1) == ["v", "type", "source", "seq", "now_ms"]
    assert (m1["seq"], m2["seq"]) == (1, 2)
    assert (m1["v"], m1["type"], m1["source"], m1["now_ms"]) == (1, "heartbeat", "SIMULATED_DEMO", 5)


def test_envelope_keeps_payload_key_order_and_leaves_payload_untouched():
    payload = bar_payload(Bar(1000, 1.0, 2.0, 0.5, 1.5, 3.0))
    before = dict(payload)
    message = Envelope(SOURCE_SIMULATED).wrap(payload)
    assert list(message) == ["v", "type", "source", "seq", "t", "o", "h", "l", "c", "vol"]
    assert payload == before


def test_envelope_keeps_its_version_on_a_series_points_message():
    payload = {"type": "series.points", "id": "series.bid", "points": {"t": [1000], "v": [1.5]}}
    message = Envelope(SOURCE_SIMULATED).wrap(payload)
    assert list(message) == ["v", "type", "source", "seq", "id", "points"]
    assert message["v"] == 1
    assert message["points"] == {"t": [1000], "v": [1.5]}


@pytest.mark.parametrize("key", ["v", "source", "seq"])
def test_envelope_rejects_a_payload_that_reuses_an_envelope_key(key):
    envelope = Envelope(SOURCE_SIMULATED)
    payload = {"type": "series.points", "id": "series.bid", key: [1]}
    with pytest.raises(ValueError, match=key):
        envelope.wrap(payload)
    assert envelope.wrap(heartbeat_payload(0))["seq"] == 1  # the failed wrap did not use a seq


def test_each_envelope_counts_its_own_seq():
    a, b = Envelope(SOURCE_SIMULATED), Envelope(SOURCE_SIMULATED)
    assert [a.wrap(heartbeat_payload(0))["seq"] for _ in range(3)] == [1, 2, 3]
    assert b.wrap(heartbeat_payload(0))["seq"] == 1


def test_encode_sse_format():
    assert encode_sse({"a": 1}) == b'data: {"a":1}\n\n'


def test_encode_keeps_non_ascii_as_utf8():
    assert encode_sse({"title": "λ₂"}) == 'data: {"title":"λ₂"}\n\n'.encode("utf-8")


def test_encode_rejects_nan():
    with pytest.raises(ValueError):
        encode_sse({"v": math.nan})


@pytest.mark.parametrize("bad", [math.inf, -math.inf])
def test_encode_rejects_infinity(bad):
    with pytest.raises(ValueError):
        encode_sse({"v": bad})


def test_ns_to_ms_floor():
    assert ns_to_ms(1_999_999) == 1
    assert ns_to_ms(2_000_000) == 2
    assert ns_to_ms(0) == 0


def test_bar_payload_maps_t_ms_to_t():
    payload = bar_payload(Bar(1_793_975_400_000, 10.0, 12.0, 9.5, 11.0, 4.5))
    assert payload == {
        "type": "bar", "t": 1_793_975_400_000, "o": 10.0, "h": 12.0, "l": 9.5, "c": 11.0, "vol": 4.5,
    }
    assert list(payload) == ["type", "t", "o", "h", "l", "c", "vol"]


def test_snapshot_payload_columnar():
    bars = [Bar(1000, 1.0, 2.0, 0.5, 1.5, 3.0), Bar(2000, 1.5, 2.5, 1.0, 2.0, 4.0)]
    overlays = [{"id": "pane.curl", "kind": "pane"}]
    points = {"series.bid": {"t": [1000], "v": [1.0]}}
    snap = snapshot_payload(2, 500, bars, overlays, points)
    assert list(snap) == ["type", "asset", "assets", "session_start_ms", "bars", "overlays", "points"]
    assert snap["type"] == "snapshot"
    assert snap["asset"] == "NVDA"
    assert snap["session_start_ms"] == 500
    assert snap["bars"] == {
        "t": [1000, 2000], "o": [1.0, 1.5], "h": [2.0, 2.5],
        "l": [0.5, 1.0], "c": [1.5, 2.0], "vol": [3.0, 4.0],
    }
    assert list(snap["bars"]) == ["t", "o", "h", "l", "c", "vol"]
    assert len(snap["assets"]) == 12
    assert snap["assets"] == [{"id": i, "symbol": s} for i, s in enumerate(ASSETS)]
    assert snap["overlays"] == overlays
    assert snap["points"] == points


def test_snapshot_payload_with_no_bars_has_empty_columns():
    snap = snapshot_payload(0, 0, [], [], {})
    assert snap["bars"] == {"t": [], "o": [], "h": [], "l": [], "c": [], "vol": []}
    assert snap["asset"] == "XLK"


def test_snapshot_assets_list_is_fresh_per_call():
    first = snapshot_payload(0, 0, [], [], {})
    first["assets"][0]["symbol"] = "CHANGED"
    assert snapshot_payload(0, 0, [], [], {})["assets"][0]["symbol"] == "XLK"


def test_heartbeat_payload():
    assert heartbeat_payload(1234) == {"type": "heartbeat", "now_ms": 1234}


def test_status_omits_none_message():
    assert "message" not in status_payload("ok", 0, None)
    assert status_payload("ok", 3, None) == {"type": "status", "feed_state": "ok", "drop_count": 3}


def test_status_keeps_message():
    payload = status_payload("degraded", 1, "RuntimeError: boom")
    assert list(payload) == ["type", "feed_state", "drop_count", "message"]
    assert payload["message"] == "RuntimeError: boom"


def test_wrapped_snapshot_round_trips_through_sse():
    snap = snapshot_payload(2, 500, [Bar(1000, 1.0, 2.0, 0.5, 1.5, 3.0)], [], {})
    raw = encode_sse(Envelope(SOURCE_SIMULATED).wrap(snap))
    assert raw.startswith(b'data: {"v":1,"type":"snapshot","source":"SIMULATED_DEMO","seq":1,"asset":"NVDA"')
    assert raw.endswith(b"\n\n")
    decoded = json.loads(raw[len(b"data: "):])
    assert decoded["bars"]["t"] == [1000]
