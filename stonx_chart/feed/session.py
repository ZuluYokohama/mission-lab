"""Rolling 1-second OHLCV bars per asset, folded from LAST trade ticks.

Standard library only.
"""

from collections import deque
from dataclasses import dataclass

from stonx_chart.feed.records import SIZE_SCALE, TICK_LAST, TickRecord


@dataclass(frozen=True, slots=True)
class Bar:
    """One 1-second bar. `t_ms` is the epoch millisecond of the second it opens."""

    t_ms: int
    o: float
    h: float
    l: float
    c: float
    vol: float


class SessionStore:
    """Per-asset rolling bars, keeping `window_s` seconds behind each asset's newest bar."""

    def __init__(self, window_s: int = 23_400) -> None:
        self._window_ms = window_s * 1000
        self._bars: dict[int, deque[Bar]] = {}
        self.stale_ticks = 0

    def apply_tick(self, tick: TickRecord) -> Bar | None:
        """Fold a LAST tick into its bar and return the updated bar.

        Returns None for any other tick type, and for a LAST tick older than the asset's
        newest bar (counted in `stale_ticks`).
        """
        if tick.tick_type != TICK_LAST:
            return None
        t_ms = (tick.epoch_ns // 1_000_000_000) * 1000
        vol = tick.size_scaled / SIZE_SCALE
        bars = self._bars.setdefault(tick.asset_id, deque())
        if not bars or t_ms > bars[-1].t_ms:
            bar = Bar(t_ms, tick.price, tick.price, tick.price, tick.price, vol)
            bars.append(bar)
            cutoff = t_ms - self._window_ms
            while bars[0].t_ms < cutoff:
                bars.popleft()
            return bar
        if t_ms < bars[-1].t_ms:
            self.stale_ticks += 1
            return None
        prev = bars[-1]
        bar = Bar(
            t_ms,
            prev.o,
            max(prev.h, tick.price),
            min(prev.l, tick.price),
            tick.price,
            prev.vol + vol,
        )
        bars[-1] = bar
        return bar

    def bars(self, asset: int) -> list[Bar]:
        """The asset's bars, oldest first."""
        return list(self._bars.get(asset, ()))
