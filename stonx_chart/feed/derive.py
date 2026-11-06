"""Overlay deriver: turns one focus asset's ticks and the signal frames into overlay ops.

One `OverlayDeriver` serves one focus asset (design spec section 7.2). Its methods return
`Op` dicts, the wire payload without an envelope:

    {"type": "overlay.upsert", "overlay": {...}}
    {"type": "overlay.remove", "id": str}
    {"type": "series.points", "id": str, "t": [int], "v": [float]}   # exactly one point

Overlay ids, groups, labels and colors are a contract with the TypeScript client. Colors
are semantic tokens only. Text never carries trade-direction wording. Every value in an op
or a stored point is finite: a non-finite input is dropped, never emitted. Ops and the
snapshots from `overlays()` and `points()` are copies, so callers cannot change the
deriver's state. Standard library only.

Time. Event times are epoch milliseconds (`epoch_ns // 1_000_000`). A point's `t` is its
second bucket, `(epoch_ns // 1_000_000_000) * 1000`. Within a second the stored value is
reduced by the series' `agg` (`last` or `max`), and every update emits the second's
reduced value, so a later op can repeat the previous `t`. A point older than the series'
newest stored second is dropped.

Curl surge. Per touching edge `e`, `n(e)` is the L2 norm of the edge's four alpha stalk
values. Each frame, `(n - mu) / sigma` is tested against 2.5 before `n` joins the window.
`mu` and `sigma` are the mean and the population standard deviation of that edge's samples
with `t >= now - 300 s`, excluding the current sample. A window with no samples, or with
`sigma` zero (up to rolling-sum rounding noise), never fires. Warm-up is 60 s from the
deriver's first frame and the cooldown is 30 s from the edge's previous marker; both are
inclusive, so a surge exactly at 60 s or exactly 30 s after the last marker can fire.
Samples are recorded on every frame, during warm-up and cooldown too. Each edge keeps
rolling sums (count, sum, sum of squares) beside its samples, so a frame costs O(touching
edges).

Eviction. `on_frame` drops stored points older than `newest frame ms - window_s * 1000`,
and removes closed bands ending before that cutoff and markers before it (an
`overlay.remove` each). Static specs and open bands stay. Ids of removed overlays are not
reused. Ops from one `on_frame` come in this order: bands, curl markers, points, removes.
"""

import math
from collections import deque
from collections.abc import Callable
from itertools import chain
from typing import Any, NamedTuple

from stonx_chart.feed.records import (
    FLAG_ERROR_317,
    FLAG_HALT,
    REGIME_FRAGMENTED,
    REGIME_HIGH_CURL,
    REGIME_HOLONOMY_SPIKE,
    SIZE_SCALE,
    TICK_BID,
    TICK_LAST,
    SignalFrame,
    TickRecord,
    edge_label,
    edge_slice,
    edges_touching,
)

Op = dict[str, Any]

_SURGE_Z = 2.5
_SURGE_WINDOW_MS = 300_000
_SURGE_WARMUP_MS = 60_000
_SURGE_COOLDOWN_MS = 30_000
# sigma counts as zero when var <= this fraction of the window's mean square. The rolling
# sums carry rounding noise of about 1e-15 of the mean square, far below this floor.
_VARIANCE_FLOOR = 1e-9


class _BandKind(NamedTuple):
    kind: str
    label: str
    color: str
    group: str


_REGIME_BANDS = (
    (REGIME_FRAGMENTED, _BandKind("fragmented", "FRAGMENTED", "regime.fragmented", "regime")),
    (REGIME_HIGH_CURL, _BandKind("high_curl", "HIGH_CURL", "regime.high_curl", "regime")),
    (REGIME_HOLONOMY_SPIKE, _BandKind("holonomy", "HOLONOMY_SPIKE", "regime.holonomy", "regime")),
)
_HALT_BAND = _BandKind("halt", "HALT", "state.halt", "halt")


def _series_spec(
    series_id: str, group: str, pane: str, style: str, color: str, width: int, agg: str, precision: int
) -> Op:
    return {
        "id": series_id, "kind": "series", "group": group, "pane": pane, "style": style,
        "color": color, "width": width, "agg": agg, "precision": precision,
    }


def _level_spec(level_id: str, value: float, label: str) -> Op:
    return {
        "id": level_id, "kind": "level", "group": "lambda2", "pane": "pane.lambda2", "value": value,
        "color": "level.threshold", "label": label, "dash": True,
    }


_STATIC_SPECS: tuple[Op, ...] = (
    {"id": "pane.lambda2", "kind": "pane", "group": "lambda2", "pane": "price",
     "title": "λ₂", "height_px": 120, "order": 1},
    {"id": "pane.curl", "kind": "pane", "group": "curl", "pane": "price",
     "title": "Star curl", "height_px": 120, "order": 2},
    _series_spec("series.lambda2", "lambda2", "pane.lambda2", "line", "series.primary", 1, "last", 3),
    _series_spec("series.star_curl", "curl", "pane.curl", "area", "series.secondary", 1, "max", 3),
    _series_spec("series.bid", "quotes", "price", "line", "series.quote", 1, "last", 2),
    _series_spec("series.ask", "quotes", "price", "line", "series.quote", 1, "last", 2),
    _series_spec("series.vwap", "vwap", "price", "line", "series.primary", 2, "last", 2),
    _level_spec("level.lambda2.fragmented", 0.05, "fragmented"),
    _level_spec("level.lambda2.coherent", 0.15, "coherent"),
)


def _upsert_op(spec: Op) -> Op:
    """An upsert op carrying its own copy of `spec`."""
    return {"type": "overlay.upsert", "overlay": dict(spec)}


_AGGREGATORS: dict[str, Callable[[float, float], float]] = {
    "last": lambda old, new: new,
    "max": max,
}


class _Series:
    """Stored points of one series: ascending second buckets, one reduced value each."""

    __slots__ = ("reduce", "t", "v")

    def __init__(self, agg: str) -> None:
        self.reduce = _AGGREGATORS[agg]
        self.t: deque[int] = deque()
        self.v: deque[float] = deque()


class _EdgeWindow:
    """Rolling n(e) samples of one edge over the last 300 s, with running sums."""

    __slots__ = ("samples", "total", "total_sq", "last_marker_ms")

    def __init__(self) -> None:
        self.samples: deque[tuple[int, float]] = deque()
        self.total = 0.0
        self.total_sq = 0.0
        self.last_marker_ms: int | None = None

    def z_score(self, now_ms: int, n: float) -> float | None:
        """z of `n` against the samples with t >= now - 300 s; None when it is undefined."""
        samples = self.samples
        cutoff = now_ms - _SURGE_WINDOW_MS
        while samples and samples[0][0] < cutoff:
            _, old = samples.popleft()
            self.total -= old
            self.total_sq -= old * old
        if not samples:
            self.total = self.total_sq = 0.0  # drop accumulated rounding error
            return None
        count = len(samples)
        mean = self.total / count
        mean_sq = self.total_sq / count
        variance = mean_sq - mean * mean
        if variance <= _VARIANCE_FLOOR * mean_sq:
            return None
        return (n - mean) / math.sqrt(variance)

    def add(self, now_ms: int, n: float) -> None:
        self.samples.append((now_ms, n))
        self.total += n
        self.total_sq += n * n


class OverlayDeriver:
    """Derives the overlay ops and 1-second series points for one focus asset."""

    def __init__(self, asset: int, session_start_ms: int, window_s: int = 23_400) -> None:
        self._asset = asset
        self._session_start_ms = session_start_ms
        self._window_ms = window_s * 1000
        self._edges = edges_touching(asset)
        self._edge_slices = tuple(edge_slice(e) for e in self._edges)
        self._edge_windows = {e: _EdgeWindow() for e in self._edges}
        self._overlays: dict[str, Op] = {spec["id"]: dict(spec) for spec in _STATIC_SPECS}
        self._series = {
            spec["id"]: _Series(spec["agg"]) for spec in _STATIC_SPECS if spec["kind"] == "series"
        }
        self._counts: dict[str, int] = {}
        self._open_bands: dict[str, str] = {}  # band kind -> id of its open band
        self._first_frame_ms: int | None = None
        self._newest_frame_ms = 0
        self._vwap_notional = 0.0
        self._vwap_size = 0.0

    # ---- public API ---------------------------------------------------------------------

    def initial_ops(self) -> list[Op]:
        """Upserts of the static specs: panes, series and levels."""
        return [_upsert_op(spec) for spec in _STATIC_SPECS]

    def on_tick(self, tick: TickRecord) -> list[Op]:
        if tick.asset_id != self._asset:
            return []
        ms = tick.epoch_ns // 1_000_000
        t = (tick.epoch_ns // 1_000_000_000) * 1000
        ops = self._update_band(_HALT_BAND, bool(tick.flags & FLAG_HALT), ms)
        if tick.flags & FLAG_ERROR_317:
            ops.append(self._add_marker("reset", ms, "above", "square", "event.reset", "Book reset"))
        if tick.tick_type == TICK_BID:
            ops.extend(self._add_point("series.bid", t, tick.price))
            ops.extend(self._add_point("series.ask", t, tick.ask_price))
        elif tick.tick_type == TICK_LAST:
            ops.extend(self._add_vwap_point(ms, t, tick))
        return ops

    def on_frame(self, frame: SignalFrame) -> list[Op]:
        ms = frame.epoch_ns // 1_000_000
        t = (frame.epoch_ns // 1_000_000_000) * 1000
        if self._first_frame_ms is None:
            self._first_frame_ms = ms
        self._newest_frame_ms = max(self._newest_frame_ms, ms)

        ops: list[Op] = []
        for bit, band in _REGIME_BANDS:
            ops.extend(self._update_band(band, bool(frame.regime_flags & bit), ms))
        stalks = [frame.alpha_coexact[s] for s in self._edge_slices]
        warmed_up = ms - self._first_frame_ms >= _SURGE_WARMUP_MS
        ops.extend(self._curl_surges(ms, warmed_up, [math.hypot(*s) for s in stalks]))
        ops.extend(self._add_point("series.lambda2", t, frame.fiedler_lambda))
        ops.extend(self._add_point("series.star_curl", t, math.hypot(*chain.from_iterable(stalks))))
        ops.extend(self._evict(self._newest_frame_ms - self._window_ms))
        return ops

    def overlays(self) -> list[Op]:
        """Copies of the live specs, in first-upsert order."""
        return [dict(spec) for spec in self._overlays.values()]

    def points(self) -> dict[str, dict[str, list]]:
        """Stored points per series, one value per second, ascending: `{id: {"t": [...], "v": [...]}}`."""
        return {sid: {"t": list(s.t), "v": list(s.v)} for sid, s in self._series.items()}

    # ---- overlays -----------------------------------------------------------------------

    def _store(self, spec: Op) -> Op:
        self._overlays[spec["id"]] = spec
        return _upsert_op(spec)

    def _next_id(self, prefix: str) -> str:
        n = self._counts[prefix] = self._counts.get(prefix, 0) + 1
        return f"{prefix}.{n}"

    def _update_band(self, band: _BandKind, active: bool, ms: int) -> list[Op]:
        open_id = self._open_bands.get(band.kind)
        if active and open_id is None:
            band_id = self._next_id(f"band.{band.kind}")
            self._open_bands[band.kind] = band_id
            return [self._store({
                "id": band_id, "kind": "band", "group": band.group, "pane": "price",
                "start_ms": ms, "end_ms": None, "color": band.color, "label": band.label,
            })]
        if not active and open_id is not None:
            del self._open_bands[band.kind]
            return [self._store({**self._overlays[open_id], "end_ms": ms})]
        return []

    def _add_marker(self, kind: str, ms: int, position: str, shape: str, color: str, text: str) -> Op:
        return self._store({
            "id": self._next_id(f"marker.{kind}"), "kind": "marker", "group": "events", "pane": "price",
            "time_ms": ms, "position": position, "shape": shape, "color": color, "text": text,
        })

    def _curl_surges(self, ms: int, warmed_up: bool, norms: list[float]) -> list[Op]:
        ops: list[Op] = []
        for edge, n in zip(self._edges, norms):
            if not math.isfinite(n):
                continue
            window = self._edge_windows[edge]
            z = window.z_score(ms, n)
            window.add(ms, n)
            if z is None or z <= _SURGE_Z or not warmed_up:
                continue
            if window.last_marker_ms is not None and ms - window.last_marker_ms < _SURGE_COOLDOWN_MS:
                continue
            window.last_marker_ms = ms
            ops.append(self._add_marker(
                "curl", ms, "below", "circle", "event.curl_surge", f"Curl surge {edge_label(edge)}"))
        return ops

    def _evict(self, cutoff_ms: int) -> list[Op]:
        for series in self._series.values():
            while series.t and series.t[0] < cutoff_ms:
                series.t.popleft()
                series.v.popleft()
        expired = [
            spec["id"] for spec in self._overlays.values()
            if (spec["kind"] == "band" and spec["end_ms"] is not None and spec["end_ms"] < cutoff_ms)
            or (spec["kind"] == "marker" and spec["time_ms"] < cutoff_ms)
        ]
        for overlay_id in expired:
            del self._overlays[overlay_id]
        return [{"type": "overlay.remove", "id": overlay_id} for overlay_id in expired]

    # ---- series points ------------------------------------------------------------------

    def _add_point(self, series_id: str, t: int, value: float) -> list[Op]:
        value = float(value)
        if not math.isfinite(value):
            return []
        series = self._series[series_id]
        if series.t and t < series.t[-1]:
            return []
        if series.t and t == series.t[-1]:
            series.v[-1] = series.reduce(series.v[-1], value)
        else:
            series.t.append(t)
            series.v.append(value)
        return [{"type": "series.points", "id": series_id, "t": [t], "v": [series.v[-1]]}]

    def _add_vwap_point(self, ms: int, t: int, tick: TickRecord) -> list[Op]:
        size = tick.size_scaled / SIZE_SCALE
        if ms < self._session_start_ms or size <= 0 or not math.isfinite(tick.price):
            return []
        self._vwap_notional += tick.price * size
        self._vwap_size += size
        return self._add_point("series.vwap", t, self._vwap_notional / self._vwap_size)
