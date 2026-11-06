"""Record types and the provisional asset universe for the StonX chart feed.

The record layouts mirror the engine's `TickEntry` and `SheafSignalEntry`
contracts (design spec section 4). Standard library only.
"""

from dataclasses import dataclass

# Asset order is the engine's asset_id order.
ASSETS: tuple[str, ...] = (
    "XLK", "SOXX", "NVDA", "QQQ", "AMD", "SPY",
    "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AVGO",
)

# PROVISIONAL (spec section 4): the engine's real 18-edge list is not available
# in this checkout. These 18 edges over the 12 assets are consistent with the
# documented neighbors of NVDA (XLK, SOXX, AMD, AVGO). Replace with the
# engine's EDGES when ShmFeedSource is built.
EDGES: tuple[tuple[int, int], ...] = (
    (2, 0), (2, 1), (2, 4), (2, 11),
    (1, 4), (1, 11),
    (0, 1), (0, 3), (0, 5), (0, 6), (0, 7),
    (3, 5), (6, 7),
    (3, 8), (3, 9), (3, 10),
    (9, 10), (7, 10),
)

# tick_type
TICK_BID = 1
TICK_ASK = 2
TICK_LAST = 4
TICK_VOLUME = 5

# TickRecord.flags
FLAG_TIER_A = 0x1
FLAG_TIER_B = 0x2
FLAG_HALT = 0x4
FLAG_ERROR_317 = 0x8

# SignalFrame.regime_flags
REGIME_FRAGMENTED = 0x1
REGIME_HIGH_CURL = 0x2
REGIME_HOLONOMY_SPIKE = 0x4

# Sizes are fixed-point integers: size = size_scaled / SIZE_SCALE.
SIZE_SCALE = 10**8

_ASSET_IDS = {symbol: i for i, symbol in enumerate(ASSETS)}


@dataclass(frozen=True, slots=True)
class TickRecord:
    """One market tick. A BID tick carries price = bid and ask_price = ask.

    A LAST tick carries ask_price = math.nan.
    """

    epoch_ns: int
    price: float
    size_scaled: int
    tick_type: int
    flags: int
    asset_id: int
    sequence: int
    ask_price: float
    ask_size_scaled: int


@dataclass(frozen=True, slots=True)
class SignalFrame:
    """One sheaf signal frame: Fiedler pair, regime flags, and edge-major stalks."""

    epoch_ns: int
    sequence: int
    fiedler_lambda: float
    regime_flags: int
    fiedler_vec: tuple[float, ...]
    alpha_coexact: tuple[float, ...]
    b_harm: tuple[float, ...]
    ols_betas: tuple[float, ...]

    def __post_init__(self) -> None:
        expected = (
            ("fiedler_vec", self.fiedler_vec, 12),
            ("alpha_coexact", self.alpha_coexact, 72),
            ("b_harm", self.b_harm, 72),
            ("ols_betas", self.ols_betas, 90),
        )
        for name, value, length in expected:
            if len(value) != length:
                raise ValueError(f"{name} must have length {length}, got {len(value)}")


def asset_id(symbol: str) -> int:
    try:
        return _ASSET_IDS[symbol]
    except KeyError:
        raise ValueError(f"unknown asset symbol: {symbol!r}") from None


def edges_touching(asset: int) -> tuple[int, ...]:
    """Indices of the edges incident to `asset`, ascending."""
    return tuple(i for i, (u, v) in enumerate(EDGES) if asset in (u, v))


def edge_label(edge: int) -> str:
    u, v = EDGES[edge]
    return f"{ASSETS[u]}–{ASSETS[v]}"


def edge_slice(edge: int) -> slice:
    """Slice of an edge's four stalk values in the edge-major alpha_coexact/b_harm arrays."""
    return slice(edge * 4, edge * 4 + 4)
