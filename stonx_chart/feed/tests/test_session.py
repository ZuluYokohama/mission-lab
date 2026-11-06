from stonx_chart.feed.session import Bar, SessionStore
from stonx_chart.feed.tests.factories import START_MS, last, quote


def test_first_trade_opens_bar():
    s = SessionStore()
    b = s.apply_tick(last(2, 0.3, 100.0, 5))
    assert b == Bar(1_793_975_400_000, 100.0, 100.0, 100.0, 100.0, 5.0)


def test_trades_in_same_second_update_ohlc():
    s = SessionStore()
    bar = None
    for t_s, price in ((0.1, 100.0), (0.4, 101.0), (0.6, 99.5), (0.9, 100.5)):
        bar = s.apply_tick(last(2, t_s, price))
    assert bar == Bar(START_MS, o=100.0, h=101.0, l=99.5, c=100.5, vol=4.0)
    assert s.bars(2) == [bar]


def test_next_second_new_bar():
    s = SessionStore()
    s.apply_tick(last(2, 0.5, 100.0))
    s.apply_tick(last(2, 1.2, 101.0))
    bars = s.bars(2)
    assert len(bars) == 2
    assert bars[1].t_ms == START_MS + 1000


def test_non_last_ticks_ignored():
    s = SessionStore()
    assert s.apply_tick(quote(2, 0.0, 99.9, 100.1)) is None
    assert s.bars(2) == []


def test_older_tick_rejected():
    s = SessionStore()
    s.apply_tick(last(2, 5.0, 100.0))
    assert s.apply_tick(last(2, 3.0, 99.0)) is None
    assert s.stale_ticks == 1
    assert [b.c for b in s.bars(2)] == [100.0]


def test_window_eviction():
    s = SessionStore(window_s=10)
    for second in range(30):
        s.apply_tick(last(2, float(second), 100.0 + second))
    bars = s.bars(2)
    assert bars[0].t_ms >= bars[-1].t_ms - 10_000
    assert bars[-1].t_ms == START_MS + 29_000
    assert len(bars) == 11


def test_assets_independent():
    s = SessionStore()
    s.apply_tick(last(2, 0.1, 100.0))
    s.apply_tick(last(4, 0.2, 50.0))
    s.apply_tick(last(2, 1.1, 101.0))
    assert [b.c for b in s.bars(2)] == [100.0, 101.0]
    assert [b.c for b in s.bars(4)] == [50.0]
    assert s.bars(7) == []
