import pytest

from stonx_chart.feed.records import (
    ASSETS,
    EDGES,
    SignalFrame,
    asset_id,
    edge_label,
    edge_slice,
    edges_touching,
)


def bfs_shells(start: int) -> list[set[int]]:
    """Hop-distance shells from `start`, treating EDGES as an undirected graph."""
    neighbors: dict[int, set[int]] = {i: set() for i in range(len(ASSETS))}
    for u, v in EDGES:
        neighbors[u].add(v)
        neighbors[v].add(u)
    seen = {start}
    shells = [{start}]
    while True:
        frontier = {n for node in shells[-1] for n in neighbors[node]} - seen
        if not frontier:
            return shells
        seen |= frontier
        shells.append(frontier)


def test_assets_order():
    assert ASSETS == ("XLK", "SOXX", "NVDA", "QQQ", "AMD", "SPY", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AVGO")


def test_edges_valid():
    assert len(EDGES) == 18
    assert len({frozenset(e) for e in EDGES}) == 18
    assert all(u != v for u, v in EDGES)
    assert all(0 <= n < len(ASSETS) for e in EDGES for n in e)
    # The graph is connected: BFS from asset 0 reaches all 12 assets.
    reached = set().union(*bfs_shells(0))
    assert reached == set(range(len(ASSETS)))


def test_nvda_hop_shells_match_engine_docs():
    assert bfs_shells(2) == [{2}, {0, 1, 4, 11}, {3, 5, 6, 7}, {8, 9, 10}]


def test_edges_touching_nvda():
    assert edges_touching(2) == (0, 1, 2, 3)


def test_edge_label():
    assert edge_label(0) == "NVDA–XLK"
    assert edge_label(17) == "MSFT–GOOGL"
    assert edge_slice(0) == slice(0, 4)
    assert edge_slice(17) == slice(68, 72)


def test_asset_id():
    assert asset_id("NVDA") == 2
    assert asset_id("XLK") == 0
    assert asset_id("AVGO") == 11
    with pytest.raises(ValueError):
        asset_id("TSLA")


def test_signal_frame_rejects_bad_lengths():
    with pytest.raises(ValueError):
        SignalFrame(0, 0, 0.2, 0, (0.0,) * 11, (0.0,) * 72, (0.0,) * 72, (0.0,) * 90)
    with pytest.raises(ValueError):
        SignalFrame(0, 0, 0.2, 0, (0.0,) * 12, (0.0,) * 71, (0.0,) * 72, (0.0,) * 90)
    with pytest.raises(ValueError):
        SignalFrame(0, 0, 0.2, 0, (0.0,) * 12, (0.0,) * 72, (0.0,) * 73, (0.0,) * 90)
    with pytest.raises(ValueError):
        SignalFrame(0, 0, 0.2, 0, (0.0,) * 12, (0.0,) * 72, (0.0,) * 72, (0.0,) * 89)
    frame = SignalFrame(0, 0, 0.2, 0, (0.0,) * 12, (0.0,) * 72, (0.0,) * 72, (0.0,) * 90)
    assert len(frame.fiedler_vec) == 12
