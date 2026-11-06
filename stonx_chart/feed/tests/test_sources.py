import math
from itertools import pairwise

import pytest

from stonx_chart.feed.records import (
    ASSETS,
    EDGES,
    FLAG_ERROR_317,
    FLAG_HALT,
    REGIME_FRAGMENTED,
    REGIME_HIGH_CURL,
    REGIME_HOLONOMY_SPIKE,
    TICK_BID,
    TICK_LAST,
    SignalFrame,
    TickRecord,
    asset_id,
)
from stonx_chart.feed.sources import (
    FRAGMENTED_THRESHOLD,
    HIGH_CURL_THRESHOLD,
    STEP_NS,
    MockFeedSource,
    SimClock,
    _CURL,
    _Schedule,
)

S = 1_000_000_000
START = 1_793_975_400_000 * 1_000_000
QUOTE_SPACING_NS = 5 * STEP_NS
NVDA = asset_id("NVDA")


def canon(records):
    """Comparable form of a record list.

    LAST ticks carry ask_price = NaN, and from Python 3.13 the dataclass `==` compares
    field by field, so `TickRecord == TickRecord` is False for any LAST tick. repr
    round-trips every float exactly and spells NaN the same way each time. Frames hold
    no NaN and compare as they are.
    """
    return [repr(r) if isinstance(r, TickRecord) else r for r in records]


@pytest.fixture(scope="module")
def recs_600():
    return MockFeedSource(7, START).advance(START + 600 * S)


@pytest.fixture(scope="module")
def frames_600(recs_600):
    return [r for r in recs_600 if isinstance(r, SignalFrame)]


def asset_ticks(recs, asset, tick_type=None):
    return [
        r
        for r in recs
        if isinstance(r, TickRecord) and r.asset_id == asset and (tick_type is None or r.tick_type == tick_type)
    ]


def test_same_seed_same_records():
    a = MockFeedSource(7, START).advance(START + 600 * S)
    b = MockFeedSource(7, START).advance(START + 600 * S)
    assert canon(a) == canon(b)


def test_different_seed_differs():
    a = MockFeedSource(7, START).advance(START + 60 * S)
    b = MockFeedSource(8, START).advance(START + 60 * S)
    assert canon(a) != canon(b)


def test_advance_is_incremental():
    a = MockFeedSource(7, START)
    first = a.advance(START + 30 * S)
    second = a.advance(START + 60 * S)
    assert first and second
    assert max(r.epoch_ns for r in first) <= START + 30 * S
    assert min(r.epoch_ns for r in second) > START + 30 * S
    assert canon(first + second) == canon(MockFeedSource(7, START).advance(START + 60 * S))
    assert a.advance(START + 60 * S) == []
    assert a.drop_count == 0


def test_records_time_ordered(recs_600):
    times = [r.epoch_ns for r in recs_600]
    assert all(a <= b for a, b in pairwise(times))
    assert times[0] == START and times[-1] == START + 600 * S
    # Tick sequences count 0, 1, 2, ... per asset.
    for asset in range(len(ASSETS)):
        sequences = [t.sequence for t in asset_ticks(recs_600, asset)]
        assert sequences == list(range(len(sequences)))


def test_one_frame_per_step():
    recs_60s = MockFeedSource(7, START).advance(START + 60 * S - 1)
    frames = [r for r in recs_60s if isinstance(r, SignalFrame)]
    assert len(frames) == 600
    assert [f.sequence for f in frames] == list(range(600))
    assert frames[0].epoch_ns == START
    assert all(b.epoch_ns - a.epoch_ns == STEP_NS for a, b in pairwise(frames))


def test_every_asset_trades_every_second_outside_halts(recs_600):
    for asset in range(len(ASSETS)):
        halted = [t.epoch_ns for t in asset_ticks(recs_600, asset, TICK_BID) if t.flags & FLAG_HALT]
        assert halted, f"{ASSETS[asset]} has no halt in the first 600 s"
        # Quotes are 0.5 s apart, so the halt began within 0.5 s before the first flagged
        # quote and ended within 0.5 s after the last one.
        halt_lo = halted[0] - QUOTE_SPACING_NS
        halt_hi = halted[-1] + QUOTE_SPACING_NS
        last_times = [t.epoch_ns for t in asset_ticks(recs_600, asset, TICK_LAST)]
        for second in range(600):
            lo = START + second * S
            hi = lo + S
            if lo < halt_hi and hi > halt_lo:
                continue
            assert any(lo <= t < hi for t in last_times), f"{ASSETS[asset]} has no LAST in second {second}"


def test_halt_has_no_trades_and_flagged_quotes(recs_600):
    quotes = asset_ticks(recs_600, NVDA, TICK_BID)
    halted = [q for q in quotes if q.flags & FLAG_HALT]
    assert len(halted) >= 30  # a 20 s halt holds about 40 quotes at 0.5 s spacing
    first, last = halted[0].epoch_ns, halted[-1].epoch_ns
    # Every quote inside the halt is flagged, and none outside it is.
    assert all(q.flags & FLAG_HALT for q in quotes if first <= q.epoch_ns <= last)
    assert not any(q.flags & FLAG_HALT for q in quotes if q.epoch_ns < first or q.epoch_ns > last)
    assert not [t for t in asset_ticks(recs_600, NVDA, TICK_LAST) if first <= t.epoch_ns <= last]


def test_nvda_events_in_first_600s(recs_600, frames_600):
    for asset in range(len(ASSETS)):
        flagged = [t for t in asset_ticks(recs_600, asset) if t.flags & FLAG_ERROR_317]
        assert len(flagged) == 1, f"{ASSETS[asset]}: expected one Error 317 tick, got {len(flagged)}"
        assert flagged[0].tick_type == TICK_BID
    assert any(t.flags & FLAG_ERROR_317 for t in asset_ticks(recs_600, NVDA))
    for bit in (REGIME_FRAGMENTED, REGIME_HIGH_CURL, REGIME_HOLONOMY_SPIKE):
        assert any(f.regime_flags & bit for f in frames_600), f"no frame with regime bit {bit:#x}"


def test_regime_flags_consistent(frames_600):
    for f in frames_600:
        expected = 0
        if f.fiedler_lambda < FRAGMENTED_THRESHOLD:
            expected |= REGIME_FRAGMENTED
        if math.hypot(*f.alpha_coexact) > HIGH_CURL_THRESHOLD:
            expected |= REGIME_HIGH_CURL
        if any(x != 0.0 for x in f.b_harm):
            expected |= REGIME_HOLONOMY_SPIKE
        assert f.regime_flags == expected, f"frame {f.sequence}"
    # Outside the scheduled events the baseline stays in regime: the mock is not permanently flagged.
    quiet = [f for f in frames_600 if f.regime_flags == 0]
    assert len(quiet) > 0.8 * len(frames_600)


def test_high_curl_holds_through_every_spike():
    recs = MockFeedSource(7, START).advance(START + 1200 * S)
    frames = [r for r in recs if isinstance(r, SignalFrame)]
    spikes = [_Schedule(7, _CURL, e) for e in range(len(EDGES))]
    inside = 0
    for f in frames:
        t_ns = f.epoch_ns - START
        in_spike = any(s.active(t_ns) for s in spikes)
        inside += in_spike
        assert bool(f.regime_flags & REGIME_HIGH_CURL) == in_spike, f"frame {f.sequence} at {t_ns / S:.1f} s"
    # Every edge spikes twice in 1200 s (about 20 frames per spike), so the check is not vacuous.
    assert inside >= 18 * 2 * 15


def test_bharm_zero_without_holonomy_flag(frames_600):
    assert all(all(x == 0.0 for x in f.b_harm) for f in frames_600 if not f.regime_flags & REGIME_HOLONOMY_SPIKE)


def test_tick_prices(recs_600):
    ticks = [r for r in recs_600 if isinstance(r, TickRecord)]
    assert {t.tick_type for t in ticks} == {TICK_BID, TICK_LAST}
    for t in ticks:
        assert 0 <= t.asset_id < len(ASSETS)
        assert t.size_scaled > 0 and isinstance(t.size_scaled, int)
        assert math.isfinite(t.price) and t.price > 0
        if t.tick_type == TICK_LAST:
            assert math.isnan(t.ask_price)
        else:
            assert math.isfinite(t.ask_price) and t.ask_price > t.price > 0
            assert t.ask_size_scaled > 0 and isinstance(t.ask_size_scaled, int)
            spread_bps = (t.ask_price - t.price) / ((t.ask_price + t.price) / 2) * 1e4
            assert 0.95 <= spread_bps <= 5.05  # 1-5 bps, give or take price rounding


def test_sim_clock_speed():
    wall = iter([0, 2 * S]).__next__
    c = SimClock(START, 10.0, wall)
    assert c.now_ns() == START + 20 * S
