"""Feed sources for the StonX chart: the `FeedSource` protocol and a deterministic mock.

`MockFeedSource` emits records shaped like the engine's `TickEntry` and
`SheafSignalEntry` contracts. Its output depends only on (seed, start_ns), so golden
fixtures can be regenerated from it. Standard library only.

Time is a fixed 100 ms grid: step k is at `start_ns + k * STEP_NS`. Each step emits, in
this order, the ticks of assets 0..11 (an asset's quote before its trades) and then one
`SignalFrame` with `sequence = k`. All records of a step share its `epoch_ns`, so
`advance` output is non-decreasing in `epoch_ns`.

Reproducibility. One `random.Random(seed)` supplies the step noise, only through
`random()` and `gauss()`. Within a step the draws come in a fixed order: market factor,
three sector factors, then per asset (return noise; on quote steps the spread and two
sizes; the trade decision and size, none while halted), then the 72 alpha components
(edge-major), the lambda_2 noise, 12 Fiedler-vector noise values, the beta drift (every
tenth step) and, while a holonomy corruption is active, the 72 `b_harm` values. The
scheduled events draw from their own per-occurrence generators (see `_Schedule`), never
from that stream. Emitted prices and signal values are rounded (to 4 and 6 decimals) so
that last-bit differences in the C math library between platforms rarely change them.
"""

import math
import random
import time
from collections.abc import Callable
from typing import NamedTuple, Protocol

from stonx_chart.feed.records import (
    ASSETS,
    EDGES,
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
    edge_slice,
)

STEP_NS = 100_000_000
FRAGMENTED_THRESHOLD = 0.05
HIGH_CURL_THRESHOLD = 1.5

_S_NS = 1_000_000_000
_PRICE_SCALE = 1e4  # prices are rounded to 4 decimals
_SIGNAL_SCALE = 1e6  # signal values are rounded to 6 decimals

# Trading cadence (in steps).
_QUOTE_EVERY = 5  # a BID quote every 0.5 s
_TRADE_EVERY = 10  # a guaranteed LAST on k % 10 == 9, so one per whole second
_EXTRA_TRADE_PROBABILITY = 0.2
_QUOTE_SPACING_NS = _QUOTE_EVERY * STEP_NS
_LOT = 100  # shares; quote and trade sizes are whole lots
_MAX_QUOTE_LOTS = 50
_MAX_TRADE_LOTS = 20
_MIN_SPREAD_BPS = 1.0
_SPREAD_RANGE_BPS = 4.0  # spread is uniform on [1, 5] bps of the mid price

# Price model. Per-step log-return = beta * market + sector + idiosyncratic noise.
# Each asset: (start price, market beta, idiosyncratic sigma, sector). Sector shocks
# are drawn once per step and shared by the sector's members.
_SEMIS, _MEGACAP, _INDEX = 0, 1, 2
_SECTOR_SIGMA = (6e-5, 4e-5, 1e-5)
_MARKET_SIGMA = 4e-5
_ASSET_MODEL = (
    (250.0, 1.1, 5e-5, _MEGACAP),  # XLK
    (300.0, 1.4, 6e-5, _SEMIS),  # SOXX
    (220.0, 1.7, 9e-5, _SEMIS),  # NVDA
    (550.0, 1.1, 2e-5, _INDEX),  # QQQ
    (180.0, 1.6, 9e-5, _SEMIS),  # AMD
    (650.0, 1.0, 1.5e-5, _INDEX),  # SPY
    (260.0, 1.0, 6e-5, _MEGACAP),  # AAPL
    (520.0, 1.0, 6e-5, _MEGACAP),  # MSFT
    (230.0, 1.2, 7e-5, _MEGACAP),  # AMZN
    (700.0, 1.3, 8e-5, _MEGACAP),  # META
    (280.0, 1.1, 7e-5, _MEGACAP),  # GOOGL
    (350.0, 1.5, 8e-5, _SEMIS),  # AVGO
)
assert len(_ASSET_MODEL) == len(ASSETS)

# Signals.
_N_EDGES = len(EDGES)
_STALK_VALUES = 4 * _N_EDGES  # length of the edge-major alpha_coexact and b_harm arrays
_ALPHA_SIGMA = 0.1
# A curl spike turns each of the edge's four baseline samples x into
# copysign(floor + gain * |x|, x). Every component is then at least 0.8, so the edge norm
# is at least 1.6 and the total norm exceeds HIGH_CURL_THRESHOLD on every spike frame (a
# plain x8 scaling would leave the edge norm near the threshold and flicker the flag).
_CURL_SPIKE_FLOOR = 0.8
_CURL_SPIKE_GAIN = 8.0
# lambda_2 is an Ornstein-Uhlenbeck walk. Each step keeps 95% of its distance from the
# target (reversion speed about 0.5 per second), and the stationary spread is 10% of the
# target: 0.02 around 0.2 normally, 0.002 around 0.02 during a fragmented dip.
_LAMBDA2_MEAN = 0.2
_LAMBDA2_FRAGMENTED_MEAN = 0.02
_LAMBDA2_RETENTION = 0.95
_LAMBDA2_NOISE = math.sqrt(1.0 - _LAMBDA2_RETENTION**2)
_LAMBDA2_REL_SIGMA = 0.1
_FIEDLER_LEN = 12
_FIEDLER_NOISE = 0.02
# Fixed ramp, zero-sum with unit norm (sum of (i - 5.5)^2 over 0..11 is 143).
_FIEDLER_BASE = tuple((i - 5.5) / math.sqrt(143.0) for i in range(_FIEDLER_LEN))
_BETAS_LEN = 90
_BETAS_BASE = tuple(0.6 + ((j * 7) % 11) * 0.08 for j in range(_BETAS_LEN))  # 0.6 .. 1.4
# OLS betas are re-estimated once per second: a slow drift around the base values,
# shared by the ten frames of that second.
_BETAS_EVERY = 10
_BETA_DRIFT_RETENTION = 0.99
_BETA_DRIFT_NOISE = 0.01
_ZERO_B_HARM = (0.0,) * _STALK_VALUES


class _EventSpec(NamedTuple):
    """A scheduled event: occurrence n starts at `offset + n * period + jitter`.

    `offset_s + key_stride_s * key` is the start of occurrence 0 for one asset or edge
    (`key`), and the jitter is uniform on [0, 20] s.
    """

    kind: str
    offset_s: int
    key_stride_s: int
    period_s: int
    duration_ns: int


_JITTER_S = 20
# The Error 317 window is one quote spacing long, so exactly one quote tick falls in it.
_ERROR_317 = _EventSpec("error317", 60, 37, 600, _QUOTE_SPACING_NS)
_HALT = _EventSpec("halt", 120, 41, 900, 20 * _S_NS)
_CURL = _EventSpec("curl", 90, 23, 600, 2 * _S_NS)
_FRAGMENTED = _EventSpec("fragmented", 420, 0, 900, 30 * _S_NS)
_HOLONOMY = _EventSpec("holonomy", 300, 0, 1200, 15 * _S_NS)


class FeedSource(Protocol):
    """The only seam between a data source and the rest of the feed."""

    drop_count: int

    def advance(self, until_ns: int) -> list[TickRecord | SignalFrame]:
        """Every record with `epoch_ns <= until_ns` not returned before, ascending by `epoch_ns`."""
        ...


class SimClock:
    """Maps wall time to simulated time: `sim_origin_ns` at creation, running `speed` times faster."""

    def __init__(self, sim_origin_ns: int, speed: float, wall_ns: Callable[[], int] = time.time_ns) -> None:
        self._sim_origin_ns = sim_origin_ns
        self._speed = speed
        self._wall_ns = wall_ns
        self._wall_at_init = wall_ns()

    def now_ns(self) -> int:
        return self._sim_origin_ns + int((self._wall_ns() - self._wall_at_init) * self._speed)


class _Schedule:
    """The windows of one `_EventSpec` for one asset or edge, relative to the feed's `start_ns`.

    The jitter of occurrence n is `random.Random(f"{seed}:{kind}:{key}:{n}").uniform(0, 20)`
    seconds, with `key` 0 for events that are not per asset or edge. A window (jitter plus
    duration, at most 50 s) is always shorter than the period (at least 600 s), so only
    occurrence `(t - offset) // period` can contain `t`. Time moves forward, so the
    current occurrence's start is cached.
    """

    __slots__ = ("_seed", "_kind", "_key", "_offset_ns", "_period_ns", "_duration_ns", "_n", "_start_ns")

    def __init__(self, seed: int, spec: _EventSpec, key: int = 0) -> None:
        self._seed = seed
        self._kind = spec.kind
        self._key = key
        self._offset_ns = (spec.offset_s + spec.key_stride_s * key) * _S_NS
        self._period_ns = spec.period_s * _S_NS
        self._duration_ns = spec.duration_ns
        self._n = -1
        self._start_ns = 0

    def active(self, t_ns: int) -> bool:
        """Whether `t_ns`, an offset from `start_ns`, lies inside an occurrence."""
        since_offset = t_ns - self._offset_ns
        if since_offset < 0:
            return False
        n = since_offset // self._period_ns
        if n != self._n:
            jitter_s = random.Random(f"{self._seed}:{self._kind}:{self._key}:{n}").uniform(0, _JITTER_S)
            self._start_ns = self._offset_ns + n * self._period_ns + round(jitter_s * _S_NS)
            self._n = n
        return self._start_ns <= t_ns < self._start_ns + self._duration_ns


def _round_price(x: float) -> float:
    return round(x * _PRICE_SCALE) / _PRICE_SCALE


def _round_signal(x: float) -> float:
    return round(x * _SIGNAL_SCALE) / _SIGNAL_SCALE


def _size_scaled(u: float, max_lots: int) -> int:
    """Fixed-point size of 1..max_lots whole lots, from a uniform draw `u` in [0, 1)."""
    return (1 + int(u * max_lots)) * _LOT * SIZE_SCALE


class MockFeedSource(FeedSource):
    """Deterministic, contract-shaped mock feed. `drop_count` is always 0.

    Magnitudes: per-step returns are about 1e-4 (a few percent over 6.5 hours), quote
    spreads are 1 to 5 bps, `alpha_coexact` components are N(0, 0.1) so the baseline
    norm is about 0.85 against the 1.5 threshold, and a curl spike on one edge lifts the
    norm above it on every frame of the spike (see `_CURL_SPIKE_FLOOR`).
    """

    def __init__(self, seed: int, start_ns: int) -> None:
        self.drop_count = 0
        self._start_ns = start_ns
        self._rng = random.Random(seed)
        self._next_step = 0
        self._mid = [model[0] for model in _ASSET_MODEL]
        self._tick_sequence = [0] * len(ASSETS)
        self._lambda2 = _LAMBDA2_MEAN
        self._beta_drift = 0.0
        self._ols_betas = _BETAS_BASE  # replaced at step 0, before the first frame is built
        self._halt = [_Schedule(seed, _HALT, a) for a in range(len(ASSETS))]
        self._error_317 = [_Schedule(seed, _ERROR_317, a) for a in range(len(ASSETS))]
        self._curl = [_Schedule(seed, _CURL, e) for e in range(_N_EDGES)]
        self._fragmented = _Schedule(seed, _FRAGMENTED)
        self._holonomy = _Schedule(seed, _HOLONOMY)

    def advance(self, until_ns: int) -> list[TickRecord | SignalFrame]:
        out: list[TickRecord | SignalFrame] = []
        last_step = (until_ns - self._start_ns) // STEP_NS
        for k in range(self._next_step, last_step + 1):
            self._emit_step(k, out)
        self._next_step = max(self._next_step, last_step + 1)
        return out

    def _emit_step(self, k: int, out: list[TickRecord | SignalFrame]) -> None:
        t_ns = k * STEP_NS
        epoch_ns = self._start_ns + t_ns
        rng = self._rng
        gauss = rng.gauss
        rand = rng.random

        market = gauss(0.0, _MARKET_SIGMA)
        sector = [gauss(0.0, sigma) for sigma in _SECTOR_SIGMA]
        quote_step = k % _QUOTE_EVERY == 0
        guaranteed_trade = k % _TRADE_EVERY == _TRADE_EVERY - 1

        for a, (_, beta, idio_sigma, sector_id) in enumerate(_ASSET_MODEL):
            mid = self._mid[a] * (1.0 + beta * market + sector[sector_id] + gauss(0.0, idio_sigma))
            self._mid[a] = mid
            halted = self._halt[a].active(t_ns)
            if quote_step:
                flags = FLAG_HALT if halted else 0
                if self._error_317[a].active(t_ns):
                    flags |= FLAG_ERROR_317
                half_spread = mid * (_MIN_SPREAD_BPS + _SPREAD_RANGE_BPS * rand()) * 0.5e-4
                out.append(
                    TickRecord(
                        epoch_ns=epoch_ns,
                        price=_round_price(mid - half_spread),
                        size_scaled=_size_scaled(rand(), _MAX_QUOTE_LOTS),
                        tick_type=TICK_BID,
                        flags=flags,
                        asset_id=a,
                        sequence=self._tick_sequence[a],
                        ask_price=_round_price(mid + half_spread),
                        ask_size_scaled=_size_scaled(rand(), _MAX_QUOTE_LOTS),
                    )
                )
                self._tick_sequence[a] += 1
            if not halted and (guaranteed_trade or rand() < _EXTRA_TRADE_PROBABILITY):
                out.append(
                    TickRecord(
                        epoch_ns=epoch_ns,
                        price=_round_price(mid),
                        size_scaled=_size_scaled(rand(), _MAX_TRADE_LOTS),
                        tick_type=TICK_LAST,
                        flags=0,
                        asset_id=a,
                        sequence=self._tick_sequence[a],
                        ask_price=math.nan,
                        ask_size_scaled=0,
                    )
                )
                self._tick_sequence[a] += 1

        out.append(self._frame(k, t_ns, epoch_ns))

    def _frame(self, k: int, t_ns: int, epoch_ns: int) -> SignalFrame:
        rng = self._rng
        gauss = rng.gauss

        alpha = [gauss(0.0, _ALPHA_SIGMA) for _ in range(_STALK_VALUES)]
        for e, schedule in enumerate(self._curl):
            if schedule.active(t_ns):
                stalks = edge_slice(e)
                alpha[stalks] = [
                    math.copysign(_CURL_SPIKE_FLOOR + _CURL_SPIKE_GAIN * abs(x), x) for x in alpha[stalks]
                ]
        alpha_coexact = tuple([_round_signal(x) for x in alpha])

        target = _LAMBDA2_FRAGMENTED_MEAN if self._fragmented.active(t_ns) else _LAMBDA2_MEAN
        self._lambda2 = (
            target
            + (self._lambda2 - target) * _LAMBDA2_RETENTION
            + _LAMBDA2_REL_SIGMA * target * _LAMBDA2_NOISE * gauss(0.0, 1.0)
        )
        fiedler_lambda = _round_signal(self._lambda2)

        fiedler_vec = tuple([_round_signal(b + gauss(0.0, _FIEDLER_NOISE)) for b in _FIEDLER_BASE])

        if k % _BETAS_EVERY == 0:
            self._beta_drift = self._beta_drift * _BETA_DRIFT_RETENTION + gauss(0.0, _BETA_DRIFT_NOISE)
            drift = self._beta_drift
            self._ols_betas = tuple([_round_signal(b + drift) for b in _BETAS_BASE])

        regime_flags = 0
        if fiedler_lambda < FRAGMENTED_THRESHOLD:
            regime_flags |= REGIME_FRAGMENTED
        if math.hypot(*alpha_coexact) > HIGH_CURL_THRESHOLD:
            regime_flags |= REGIME_HIGH_CURL
        b_harm = _ZERO_B_HARM
        if self._holonomy.active(t_ns):
            # Corruption: every harmonic component is pushed to a non-zero value in [0.25, 0.75).
            rand = rng.random
            b_harm = tuple([_round_signal(0.25 + 0.5 * rand()) for _ in range(_STALK_VALUES)])
            regime_flags |= REGIME_HOLONOMY_SPIKE

        return SignalFrame(
            epoch_ns=epoch_ns,
            sequence=k,
            fiedler_lambda=fiedler_lambda,
            regime_flags=regime_flags,
            fiedler_vec=fiedler_vec,
            alpha_coexact=alpha_coexact,
            b_harm=b_harm,
            ols_betas=self._ols_betas,
        )
