"""Shared test factories for the feed tests.

`t_s` is float seconds after the session start (`START_MS`). Sequence numbers come from
one module-level counter, so records built in order carry increasing sequences.
"""

import math
from itertools import count

from stonx_chart.feed.records import SIZE_SCALE, TICK_BID, TICK_LAST, SignalFrame, TickRecord

START_MS = 1_793_975_400_000
START_NS = START_MS * 1_000_000

_sequence = count()


def _epoch_ns(t_s: float) -> int:
    return START_NS + round(t_s * 1_000_000_000)


def last(asset: int, t_s: float, price: float, size: float = 1.0, flags: int = 0) -> TickRecord:
    """A LAST trade tick. It carries ask_price = NaN, so never compare two of these with `==`."""
    return TickRecord(
        epoch_ns=_epoch_ns(t_s),
        price=price,
        size_scaled=round(size * SIZE_SCALE),
        tick_type=TICK_LAST,
        flags=flags,
        asset_id=asset,
        sequence=next(_sequence),
        ask_price=math.nan,
        ask_size_scaled=0,
    )


def quote(asset: int, t_s: float, bid: float, ask: float, flags: int = 0) -> TickRecord:
    """A BID tick: price = bid, ask_price = ask."""
    return TickRecord(
        epoch_ns=_epoch_ns(t_s),
        price=bid,
        size_scaled=100 * SIZE_SCALE,
        tick_type=TICK_BID,
        flags=flags,
        asset_id=asset,
        sequence=next(_sequence),
        ask_price=ask,
        ask_size_scaled=100 * SIZE_SCALE,
    )


def frame(
    t_s: float,
    lam: float = 0.2,
    flags: int = 0,
    alpha: tuple[float, ...] | None = None,
    b_harm: tuple[float, ...] | None = None,
) -> SignalFrame:
    """A SignalFrame; alpha and b_harm default to 72 zeros, the other arrays are zeros."""
    return SignalFrame(
        epoch_ns=_epoch_ns(t_s),
        sequence=next(_sequence),
        fiedler_lambda=lam,
        regime_flags=flags,
        fiedler_vec=(0.0,) * 12,
        alpha_coexact=alpha if alpha is not None else (0.0,) * 72,
        b_harm=b_harm if b_harm is not None else (0.0,) * 72,
        ols_betas=(0.0,) * 90,
    )
