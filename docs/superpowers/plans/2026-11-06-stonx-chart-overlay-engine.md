# StonX Chart & Overlay Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A loopback web chart of one StonX focus asset: live 1s/5s/1m/5m candles plus server-defined overlays (bands, markers, sub-panes, series, levels), fed by a deterministic `SIMULATED_DEMO` mock.

**Architecture:** A standard-library Python package (`stonx_chart/feed`) generates contract-shaped ticks and signal frames, folds them into 1-second bars and overlay operations, and streams one asset per SSE connection (snapshot first, then increments). A TypeScript app (`stonx_chart/web`) decodes the stream into a `BarStore` and an `OverlayStore`, re-buckets everything to the active interval, and flushes changes once per animation frame through a `ChartSurface` interface. `LwcSurface` implements that interface with Lightweight Charts.

**Tech Stack:** Python 3.12 (stdlib + pytest), TypeScript, Vite, Vitest, Playwright (Chromium), lightweight-charts 5.2.1.

**Spec:** `docs/superpowers/specs/2026-11-06-stonx-chart-overlay-engine-design.md`

## Global Constraints

- Git: stage files by explicit path only. Never `git add -A`, `git add .` or `git commit -a`. The working tree has unrelated uncommitted deletions (`research/`, `docs/*.md`, `tools/`) that must stay out of every commit.
- Python: the feed's runtime is standard library only, and code must run on Python 3.12 (CI). This machine only has Python 3.14, so don't use APIs added after 3.12.
- Python commands below are written `python …`. Locally, run them as `.venv\Scripts\python.exe …` once Task 1 has rebuilt the venv.
- Node ≥ 22.12; CI uses Node 22. Run npm commands from `stonx_chart/web`.
- `lightweight-charts` is pinned exactly to `5.2.1`. Only files named `stonx_chart/web/src/surface/lwc*.ts` may import it.
- The server binds `127.0.0.1` only and serves GET only; every other method returns 405.
- Every wire message carries `v: 1`, `type`, `source` and `seq`. `seq` starts at 1 on each connection and increases by 1. The mock's source string is `SIMULATED_DEMO`.
- Wire times are integer milliseconds since the epoch. JSON is encoded with `allow_nan=False`.
- Intervals are exactly 1, 5, 60 and 300 s. Bucket = `floor(t_ms / (I*1000)) * I*1000`.
- Fixed values: heartbeat 1 s; stale after 3 s with no message; reconnect backoff 500 ms doubling to a 5,000 ms cap, reset on snapshot; client queue 2,000 messages; slot flush every 50 ms; window 23,400 s; client trims at 110% of the window.
- Overlay `text` and `label` never contain "BUY" or "SELL" (case-insensitive).
- Wire colors are semantic tokens only: `regime.fragmented`, `regime.high_curl`, `regime.holonomy`, `state.halt`, `event.reset`, `event.curl_surge`, `series.primary`, `series.secondary`, `series.quote`, `level.threshold`, `neutral`.
- Every `localStorage` access is wrapped in try/catch. Times display in `America/New_York`.
- Each golden fixture file is ≤ 256 KiB.

## Refinements to the spec

These settle choices the spec left open. Executors follow them.

1. **Dense candle grid.** Candles at interval `I` contain one item per bucket from the first to the last bucket holding any bar or series point. Buckets without a bar get a whitespace item. Gaps (halts) therefore occupy real width, and band edges always have a time coordinate. Markers still snap to buckets that hold a real bar (spec §7.1).
2. **`StreamHub` and `ClientChannel` live in `hub.py`;** `server.py` is HTTP only.
3. **`ChartSurface` method list** (Task 11) replaces the op names in spec §8.
4. **`pane` overlays carry `"pane": "price"`,** which is ignored, so every overlay has the same base fields.
5. **Server `drop_count`** is the number of messages discarded for this connection by resyncs.
6. **Defaults:** asset `NVDA`, interval 60 s.
7. **Node 22** rather than 24 (the installed version; the spec is updated in Task 16).

## Review Focus

1. **Halt at 1s:** no trades for 20 s, but signal points keep arriving. The candles should show an empty stretch with the HALT band visible across it. → Task 11, `halt gap renders whitespace and band`.
2. **Trade ticks carry `ask_price = NaN`.** A NaN must never reach the bid/ask series or the encoder; `allow_nan=False` would crash the pump. → Task 4, `test_bid_ask_ignore_trade_ticks_and_stay_finite`.
3. **Asset switch while a reconnect is pending:** the old asset's timer or late events must not open a stream or leak data into the new asset. → Task 12, `asset switch cancels pending reconnect and ignores old stream`.
4. **Background tab:** `requestAnimationFrame` pauses while thousands of messages arrive. On return, one flush applies them with surface calls bounded by distinct buckets. → Task 11, `burst of messages collapses into bounded surface calls`.
5. **Page open longer than the window:** memory stays bounded by trimming, and live-edge follow survives the trim. → Task 11, `trims past 110% of window and keeps live edge`.

---

### Task 1: Package scaffold and record types

**Files:**
- Create: `stonx_chart/__init__.py`, `stonx_chart/feed/__init__.py`, `stonx_chart/feed/records.py`, `stonx_chart/feed/tests/__init__.py`, `stonx_chart/feed/tests/test_records.py`

**Interfaces:**
- Produces (in `stonx_chart.feed.records`):
  - `ASSETS: tuple[str, ...]`: `("XLK","SOXX","NVDA","QQQ","AMD","SPY","AAPL","MSFT","AMZN","META","GOOGL","AVGO")`
  - `EDGES: tuple[tuple[int, int], ...]`: exactly `((2,0),(2,1),(2,4),(2,11),(1,4),(1,11),(0,1),(0,3),(0,5),(0,6),(0,7),(3,5),(6,7),(3,8),(3,9),(3,10),(9,10),(7,10))`, with a comment marking it provisional (spec §4)
  - `TICK_BID=1, TICK_ASK=2, TICK_LAST=4, TICK_VOLUME=5`; `FLAG_TIER_A=0x1, FLAG_TIER_B=0x2, FLAG_HALT=0x4, FLAG_ERROR_317=0x8`; `REGIME_FRAGMENTED=0x1, REGIME_HIGH_CURL=0x2, REGIME_HOLONOMY_SPIKE=0x4`; `SIZE_SCALE = 10**8`
  - `@dataclass(frozen=True, slots=True) TickRecord(epoch_ns: int, price: float, size_scaled: int, tick_type: int, flags: int, asset_id: int, sequence: int, ask_price: float, ask_size_scaled: int)`. A BID tick carries price = bid and ask_price = ask. A LAST tick carries ask_price = `math.nan`.
  - `@dataclass(frozen=True, slots=True) SignalFrame(epoch_ns: int, sequence: int, fiedler_lambda: float, regime_flags: int, fiedler_vec: tuple[float, ...], alpha_coexact: tuple[float, ...], b_harm: tuple[float, ...], ols_betas: tuple[float, ...])`. `__post_init__` raises `ValueError` unless the lengths are 12/72/72/90.
  - `asset_id(symbol: str) -> int` (`ValueError` if unknown); `edges_touching(asset: int) -> tuple[int, ...]` (ascending edge indices); `edge_label(edge: int) -> str` (`f"{ASSETS[u]}–{ASSETS[v]}"` with an en dash); `edge_slice(edge: int) -> slice` (`slice(edge*4, edge*4+4)`)

- [ ] **Step 1: Rebuild the venv.** The current `.venv` is an empty shell. Run `python -m venv --clear .venv`, then `.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`. Expected: `.venv\Scripts\python.exe -m pytest --version` prints `pytest 9.2.0`.

- [ ] **Step 2: Write the failing tests**

```python
def test_assets_order(): assert ASSETS == ("XLK","SOXX","NVDA","QQQ","AMD","SPY","AAPL","MSFT","AMZN","META","GOOGL","AVGO")
def test_edges_valid(): assert len(EDGES) == 18; assert len({frozenset(e) for e in EDGES}) == 18; assert all(u != v for u, v in EDGES)  # plus BFS from 0 reaches all 12
def test_nvda_hop_shells_match_engine_docs():
    assert bfs_shells(2) == [{2}, {0, 1, 4, 11}, {3, 5, 6, 7}, {8, 9, 10}]  # bfs_shells is a test-local helper
def test_edges_touching_nvda(): assert edges_touching(2) == (0, 1, 2, 3)
def test_edge_label(): assert edge_label(0) == "NVDA–XLK"
def test_asset_id(): assert asset_id("NVDA") == 2; pytest.raises(ValueError, asset_id, "TSLA")
def test_signal_frame_rejects_bad_lengths(): pytest.raises(ValueError, SignalFrame, 0, 0, 0.2, 0, (0.0,)*11, (0.0,)*72, (0.0,)*72, (0.0,)*90)
```

- [ ] **Step 3: Run** `python -B -m pytest stonx_chart/feed/tests/test_records.py -v`. Expected: FAIL (ImportError).
- [ ] **Step 4: Implement `records.py`** as specified in Interfaces.
- [ ] **Step 5: Run the same command.** Expected: 7 passed.
- [ ] **Step 6: Commit** `stonx_chart/__init__.py stonx_chart/feed/__init__.py stonx_chart/feed/records.py stonx_chart/feed/tests/__init__.py stonx_chart/feed/tests/test_records.py` with message `feat(stonx_chart): add feed record types and provisional universe`.

---

### Task 2: Deterministic mock feed

**Files:**
- Create: `stonx_chart/feed/sources.py`, `stonx_chart/feed/tests/test_sources.py`

**Interfaces:**
- Consumes: Task 1 records and constants.
- Produces:
  - `class FeedSource(Protocol)`: `advance(self, until_ns: int) -> list[TickRecord | SignalFrame]` returns every record with `epoch_ns <= until_ns` not returned before, ascending by `epoch_ns`. Also `drop_count: int`.
  - `class MockFeedSource(FeedSource)`: `__init__(self, seed: int, start_ns: int)`; `drop_count` is always 0.
  - `class SimClock`: `__init__(self, sim_origin_ns: int, speed: float, wall_ns: Callable[[], int] = time.time_ns)`; `now_ns() -> int` = `sim_origin_ns + int((wall_ns() - wall_at_init) * speed)`.
  - Constants `STEP_NS = 100_000_000`, `FRAGMENTED_THRESHOLD = 0.05`, `HIGH_CURL_THRESHOLD = 1.5`.

**Mock rules.** The implementer picks the magnitudes not listed here.
- Step `k` is at `start_ns + k*STEP_NS`. Each step emits exactly one `SignalFrame` with `sequence = k`.
- For each asset: a BID quote tick at steps with `k % 5 == 0`, with a spread of 1–5 bps and `ask > bid`. A LAST trade at `k % 10 == 9`, plus extra LAST trades with probability 0.2 at other steps. During the asset's halt there are no LAST trades, and its quote ticks carry `FLAG_HALT`.
- Prices follow market + sector + idiosyncratic returns and stay positive.
- **Schedule.** Times are offsets from `start_ns` in seconds; occurrence `n` is at `offset + n*period + jitter`. `jitter = random.Random(f"{seed}:{kind}:{key}:{n}").uniform(0, 20)`.
  - Error 317: offset `60 + 37*asset`, period 600. One quote tick for that asset carries `FLAG_ERROR_317`.
  - Halt: offset `120 + 41*asset`, period 900, lasting 20 s.
  - Curl spike on edge `e`: offset `90 + 23*e`, period 600, lasting 2 s. The edge's 4 α components are multiplied by 8.
  - Fragmented dip: offset 420, period 900, lasting 30 s. λ₂ reverts toward 0.02 instead of 0.2.
  - Holonomy corruption: offset 300, period 1200, lasting 15 s. `b_harm` is non-zero.
- Baseline α components ~ N(0, 0.1). λ₂ is an OU walk around 0.2. `b_harm` is all zeros outside corruption windows.
- `regime_flags`: FRAGMENTED iff `fiedler_lambda < 0.05`; HIGH_CURL iff ‖alpha_coexact‖₂ > 1.5; HOLONOMY_SPIKE iff any `b_harm` ≠ 0.

- [ ] **Step 1: Write the failing tests.** `S = 1_000_000_000` and `START = 1_793_975_400_000 * 1_000_000`.

```python
def test_same_seed_same_records(): assert MockFeedSource(7, START).advance(START+600*S) == MockFeedSource(7, START).advance(START+600*S)
def test_different_seed_differs(): assert MockFeedSource(7, START).advance(START+60*S) != MockFeedSource(8, START).advance(START+60*S)
def test_advance_is_incremental(): a = MockFeedSource(7, START); assert a.advance(START+30*S) + a.advance(START+60*S) == MockFeedSource(7, START).advance(START+60*S)
def test_records_time_ordered(): ...  # epoch_ns non-decreasing over 600 s
def test_one_frame_per_step(): frames = [r for r in recs_60s if isinstance(r, SignalFrame)]; assert len(frames) == 600 and all(b.epoch_ns - a.epoch_ns == STEP_NS for a, b in pairwise(frames))
def test_every_asset_trades_every_second_outside_halts(): ...  # for each asset and each whole second of 600 s not inside its halt: ≥1 LAST
def test_halt_has_no_trades_and_flagged_quotes(): ...  # NVDA: halt quotes exist in first 600 s; no LAST between first and last halt-flagged quote
def test_nvda_events_in_first_600s(): ...  # ≥1 NVDA tick with FLAG_ERROR_317; frames exist with each of the 3 regime bits set
def test_regime_flags_consistent(): ...  # every frame: bits equal the three conditions above
def test_bharm_zero_without_holonomy_flag(): assert all(all(x == 0.0 for x in f.b_harm) for f in frames if not f.regime_flags & REGIME_HOLONOMY_SPIKE)
def test_tick_prices(): ...  # LAST: math.isnan(ask_price); BID: finite price and ask_price, ask_price > price > 0
def test_sim_clock_speed(): wall = iter([0, 2*S]).__next__; c = SimClock(START, 10.0, wall); assert c.now_ns() == START + 20*S
```

- [ ] **Step 2: Run** `python -B -m pytest stonx_chart/feed/tests/test_sources.py -v`. Expected: FAIL (ImportError).
- [ ] **Step 3: Implement `sources.py`.** Use one `random.Random(seed)` for step noise, consumed in a fixed order (assets ascending, then edges), so output is reproducible.
- [ ] **Step 4: Run the same command.** Expected: 12 passed.
- [ ] **Step 5: Commit** `stonx_chart/feed/sources.py stonx_chart/feed/tests/test_sources.py` with message `feat(stonx_chart): add deterministic contract-shaped mock feed`.

---

### Task 3: Session bars

**Files:**
- Create: `stonx_chart/feed/session.py`, `stonx_chart/feed/tests/factories.py`, `stonx_chart/feed/tests/test_session.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True, slots=True) Bar(t_ms: int, o: float, h: float, l: float, c: float, vol: float)`
  - `class SessionStore`: `__init__(self, window_s: int = 23_400)`
    - `apply_tick(self, tick: TickRecord) -> Bar | None`: LAST ticks only. Returns the updated bar for `t_ms = (epoch_ns // 1_000_000_000) * 1000`. `vol` adds `size_scaled / SIZE_SCALE`. Non-LAST ticks return None. A LAST tick older than the asset's newest bar second returns None and increments `stale_ticks`. After each update, the asset's bars with `t_ms < newest - window_s*1000` are dropped.
    - `bars(self, asset: int) -> list[Bar]` (ascending); `stale_ticks: int`
  - Test factories in `factories.py`: `last(asset, t_s, price, size=1.0, flags=0)`, `quote(asset, t_s, bid, ask, flags=0)`, `frame(t_s, lam=0.2, flags=0, alpha=None, b_harm=None)`. `t_s` is float seconds after the start; `alpha` is a 72-tuple, zeros by default. `START_MS = 1_793_975_400_000`, `START_NS = START_MS * 1_000_000`.

- [ ] **Step 1: Write the failing tests**

```python
def test_first_trade_opens_bar(): s = SessionStore(); b = s.apply_tick(last(2, 0.3, 100.0, 5)); assert b == Bar(1_793_975_400_000, 100.0, 100.0, 100.0, 100.0, 5.0)
def test_trades_in_same_second_update_ohlc(): ...  # 100, 101, 99.5, 100.5 at 0.1..0.9 s → Bar(o=100, h=101, l=99.5, c=100.5, vol=4)
def test_next_second_new_bar(): ...  # second trade at 1.2 s → len(bars(2)) == 2 and bars(2)[1].t_ms == START_MS + 1000
def test_non_last_ticks_ignored(): assert SessionStore().apply_tick(quote(2, 0.0, 99.9, 100.1)) is None
def test_older_tick_rejected(): ...  # trade at 5 s then at 3 s → None, stale_ticks == 1
def test_window_eviction(): ...  # window_s=10, one trade per second for 30 s → bars(2)[0].t_ms >= bars(2)[-1].t_ms - 10_000
def test_assets_independent(): ...  # trades on 2 and 4 → each asset's bars contain only its own prices
```

- [ ] **Step 2: Run** `python -B -m pytest stonx_chart/feed/tests/test_session.py -v`. Expected: FAIL (ImportError).
- [ ] **Step 3: Implement `session.py` and `factories.py`.**
- [ ] **Step 4: Run the same command.** Expected: 7 passed.
- [ ] **Step 5: Commit** `stonx_chart/feed/session.py stonx_chart/feed/tests/factories.py stonx_chart/feed/tests/test_session.py` with message `feat(stonx_chart): fold ticks into rolling 1s bars`.

---

### Task 4: Overlay deriver

**Files:**
- Create: `stonx_chart/feed/derive.py`, `stonx_chart/feed/tests/test_derive.py`

**Interfaces:**
- Consumes: Task 1 records and helpers (the deriver reads regime flag bits; it never re-applies the mock's thresholds); Task 3 factories; `MockFeedSource` (Task 2) in one test only.
- Produces: `Op = dict[str, Any]`, the wire payload without an envelope:
  - `{"type": "overlay.upsert", "overlay": {...}}`
  - `{"type": "overlay.remove", "id": str}`
  - `{"type": "series.points", "id": str, "t": [int], "v": [float]}` (one point per op)
- `class OverlayDeriver`: `__init__(self, asset: int, session_start_ms: int, window_s: int = 23_400)`
  - `initial_ops(self) -> list[Op]`: upserts, in this order:
    - `pane.lambda2` (group `lambda2`, title `λ₂`, height_px 120, order 1)
    - `pane.curl` (group `curl`, title `Star curl`, height_px 120, order 2)
    - `series.lambda2` (pane `pane.lambda2`, line, `series.primary`, width 1, agg `last`, precision 3)
    - `series.star_curl` (pane `pane.curl`, area, `series.secondary`, width 1, agg `max`, precision 3)
    - `series.bid` and `series.ask` (pane `price`, group `quotes`, line, `series.quote`, width 1, agg `last`, precision 2)
    - `series.vwap` (pane `price`, group `vwap`, line, `series.primary`, width 2, agg `last`, precision 2)
    - `level.lambda2.fragmented` (pane `pane.lambda2`, group `lambda2`, value 0.05, `level.threshold`, label `fragmented`, dash true)
    - `level.lambda2.coherent` (pane `pane.lambda2`, group `lambda2`, value 0.15, `level.threshold`, label `coherent`, dash true)
    - All `pane` overlays carry `"pane": "price"`.
  - `on_tick(self, tick: TickRecord) -> list[Op]`: ignores ticks for other assets.
  - `on_frame(self, frame: SignalFrame) -> list[Op]`: also evicts. Points older than `newest_ms - window_s*1000` are dropped; bands with `end_ms` and markers with `time_ms` before that cutoff get an `overlay.remove`.
  - `overlays(self) -> list[dict]`: live specs in first-upsert order.
  - `points(self) -> dict[str, dict[str, list]]`: `{series_id: {"t": [...], "v": [...]}}` at 1s.
- **Bands.** Ids are `band.<kind>.<n>`, with `n` counting from 1 per kind. All bands are in pane `price`.

  | Kind | Label | Color | Group | Trigger |
  |---|---|---|---|---|
  | `fragmented` | `FRAGMENTED` | `regime.fragmented` | `regime` | regime bit 0x1 |
  | `high_curl` | `HIGH_CURL` | `regime.high_curl` | `regime` | regime bit 0x2 |
  | `holonomy` | `HOLONOMY_SPIKE` | `regime.holonomy` | `regime` | regime bit 0x4 |
  | `halt` | `HALT` | `state.halt` | `halt` | the asset's `FLAG_HALT` |

  A rising edge upserts the band with `start_ms` = event ms and `end_ms` null. A falling edge re-upserts it with `end_ms` = event ms. For halt, a tick without `FLAG_HALT` is a falling edge.
- **Markers.** Group `events`, pane `price`.
  - `marker.reset.<n>`: one per tick with `FLAG_ERROR_317`. Position `above`, shape `square`, `event.reset`, text `Book reset`.
  - `marker.curl.<n>`: position `below`, shape `circle`, `event.curl_surge`, text `f"Curl surge {edge_label(e)}"`. For each edge `e` in `edges_touching(asset)`, `n(e) = ‖alpha[edge_slice(e)]‖₂`. Fire when `(n − μ)/σ > 2.5`, where μ and σ come from that edge's samples in the preceding 300 s, excluding the current one. Never fire if σ == 0, before 60 s after the first frame, or within 30 s of that edge's previous marker.
- **Points.** Reduce within each second by the series' `agg` and emit the current second's reduced value on every update.
  - `series.lambda2`: `fiedler_lambda`.
  - `series.star_curl`: ‖concatenated `alpha[edge_slice(e)]` over the touching edges‖₂.
  - `series.bid` / `series.ask`: BID ticks only.
  - `series.vwap`: Σ(price·size)/Σ(size) over LAST ticks since `session_start_ms`.

- [ ] **Step 1: Write the failing tests** (use the Task 3 factories; asset 2 unless noted)

```python
def test_initial_ops_exact(): ids = [op["overlay"]["id"] for op in OverlayDeriver(2, START_MS).initial_ops()]; assert ids == ["pane.lambda2","pane.curl","series.lambda2","series.star_curl","series.bid","series.ask","series.vwap","level.lambda2.fragmented","level.lambda2.coherent"]
def test_regime_band_opens_and_closes(): ...  # frames: flags 0 @0s, 0x1 @1s, 0 @2.5s → upsert band.fragmented.1 end_ms None, then end_ms == START_MS + 2500
def test_second_band_new_id(): ...  # two separate FRAGMENTED spans → band.fragmented.1 and band.fragmented.2
def test_halt_band_from_ticks(): ...  # quote with FLAG_HALT @10s, quote without @30s → band.halt.1 start START_MS+10000, end START_MS+30000; halt ticks of asset 4 produce nothing
def test_reset_marker(): ...  # quote(2, 5.0, ..., flags=FLAG_ERROR_317) → marker.reset.1 time_ms START_MS+5000, text "Book reset"
def test_curl_surge_after_warmup(): ...  # baseline alpha σ=0.1 noise (seeded) every 100 ms; edge-0 ×8 spike at 30 s → no marker; at 90 s → marker.curl.1 text "Curl surge NVDA–XLK"
def test_curl_surge_cooldown(): ...  # spikes at 90 s and 100 s → one marker; at 90 s and 125 s → two
def test_curl_ignores_other_edges(): ...  # spike on edge 10 only → no curl marker for asset 2
def test_points_reduce_by_agg(): ...  # λ₂ 0.3 then 0.1 within one second → last point v == 0.1; star curl 2.0 then 1.0 → v == 2.0
def test_bid_ask_ignore_trade_ticks_and_stay_finite(): ...  # quotes plus LAST ticks (ask NaN) → bid/ask points equal the quotes; every emitted v is finite
def test_vwap(): ...  # trades (100, 1), (102, 3) → vwap point 101.5
def test_no_buy_sell_text(): ...  # run all 12 derivers over MockFeedSource(7) for 600 s; no overlay label/text matches /buy|sell/i
def test_eviction_removes_old_overlays(): ...  # window_s=60: closed band ending at 10 s, frame at 80 s → overlay.remove for it
def test_overlays_and_points_snapshot(): ...  # overlays() contains the open band with end_ms None; points()["series.lambda2"]["t"] is ascending at 1000 ms steps
```

- [ ] **Step 2: Run** `python -B -m pytest stonx_chart/feed/tests/test_derive.py -v`. Expected: FAIL (ImportError).
- [ ] **Step 3: Implement `derive.py`.** Keep rolling sums per edge (Σn, Σn², deque of (t, n)) so each frame costs O(touching edges).
- [ ] **Step 4: Run the same command.** Expected: 14 passed.
- [ ] **Step 5: Commit** `stonx_chart/feed/derive.py stonx_chart/feed/tests/test_derive.py` with message `feat(stonx_chart): derive declarative overlays from signals and ticks`.

---

### Task 5: Wire encoding and stream hub

**Files:**
- Create: `stonx_chart/feed/wire.py`, `stonx_chart/feed/hub.py`, `stonx_chart/feed/tests/test_wire.py`, `stonx_chart/feed/tests/test_hub.py`

**Interfaces:**
- Consumes: `FeedSource` (Task 2), `SessionStore` and `Bar` (Task 3), `OverlayDeriver` and `Op` (Task 4).
- Produces (`wire.py`):
  - `SCHEMA_VERSION = 1`, `SOURCE_SIMULATED = "SIMULATED_DEMO"`; `ns_to_ms(ns: int) -> int` (floor)
  - `bar_payload(bar: Bar) -> dict` → `{"type":"bar","t","o","h","l","c","vol"}`
  - `snapshot_payload(asset: int, session_start_ms: int, bars: list[Bar], overlays: list[dict], points: dict) -> dict` → `{"type":"snapshot","asset":"NVDA","assets":[{"id":0,"symbol":"XLK"},…],"session_start_ms","bars":{"t":[],"o":[],"h":[],"l":[],"c":[],"vol":[]},"overlays","points"}`
  - `heartbeat_payload(now_ms: int) -> dict`; `status_payload(feed_state: str, drop_count: int, message: str | None) -> dict` (omits `message` when None)
  - `class Envelope`: `__init__(self, source: str)`; `wrap(self, payload: dict) -> dict`. Key order: `v, type, source, seq`, then the payload's other keys. `seq` starts at 1.
  - `encode_sse(message: dict) -> bytes` → `b"data: " + json.dumps(message, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode() + b"\n\n"`
- Produces (`hub.py`):
  - `class ClientChannel`: `__init__(self, asset: int, max_queue: int = 2000)`; `offer(self, payload: dict) -> None`; `flush_slots(self) -> None`; `take(self, timeout_s: float) -> list[dict]` (waits up to the timeout for queued messages, returns and clears them); `needs_snapshot: bool`; `discarded: int`. Thread-safe with its own lock and condition.
  - **Coalescing.** `bar` uses slot key `"bar"`; `series.points` uses `("points", id)`. If the slot holds a payload with the same `t`, replace it. Otherwise move the held payload to the queue and hold the new one. `flush_slots` moves held payloads to the queue sorted by `t` ascending. Every other type goes straight to the queue. If the queue exceeds `max_queue`: clear the queue and slots, add the cleared count to `discarded`, and set `needs_snapshot = True`.
  - `class StreamHub`: `__init__(self, source: FeedSource, session_start_ns: int, window_s: int = 23_400)`. It builds one `SessionStore` and 12 `OverlayDerivers`, and stores each deriver's `initial_ops()`.
    - `pump(self, until_ns: int) -> None`: advance the source and route records to the store and derivers, offering resulting payloads to channels of that asset. Ticks go to the deriver for `tick.asset_id`; frames go to all 12. If `source.advance` raises: `feed_state = "degraded"`, `status_message = f"{type(e).__name__}: {e}"`. The next success sets `"ok"` and None. On every change, offer `status_payload` (using each channel's `discarded`) to all channels.
    - `subscribe(self, asset: int) -> tuple[ClientChannel, dict]`: registers the channel and builds the snapshot under the hub lock.
    - `unsubscribe(self, channel)`; `resync(self, channel) -> dict` (clears the channel, sets `needs_snapshot = False`, returns a fresh snapshot); `snapshot(self, asset) -> dict`; `flush_all(self)`; `channel_count(self) -> int`; `feed_state: str`; `status_message: str | None`; `session_start_ms: int`
    - `pump`, `subscribe` and `resync` share one `threading.Lock`.

- [ ] **Step 1: Write the failing tests**

```python
# test_wire.py
def test_envelope_order_and_seq(): e = Envelope("SIMULATED_DEMO"); m1 = e.wrap(heartbeat_payload(5)); m2 = e.wrap(heartbeat_payload(6)); assert list(m1) == ["v","type","source","seq","now_ms"] and (m1["seq"], m2["seq"]) == (1, 2)
def test_encode_sse_format(): assert encode_sse({"a": 1}) == b'data: {"a":1}\n\n'
def test_encode_rejects_nan(): pytest.raises(ValueError, encode_sse, {"v": math.nan})
def test_ns_to_ms_floor(): assert ns_to_ms(1_999_999) == 1
def test_snapshot_payload_columnar(): ...  # two Bars → bars["t"] == [t1, t2], assets has 12 entries, asset == "NVDA"
def test_status_omits_none_message(): assert "message" not in status_payload("ok", 0, None)
# test_hub.py
def test_snapshot_then_only_own_asset(): ...  # hub over MockFeedSource(7); pump 60 s; subscribe(2); pump +1 s; flush_all; take(0) → non-empty, every bar/series.points is for NVDA (compare against SessionStore bars of asset 2)
def test_coalesce_same_t_replaced(): ...  # offer bar t=1 twice → one message after flush_slots
def test_coalesce_new_t_pushes_previous(): ...  # bar t=1 then t=2 → both delivered, t=1 first
def test_flush_orders_slots_by_t(): ...  # points t=2 then bar t=1 → after flush: bar(t=1) before points(t=2)
def test_overlay_ops_not_coalesced(): ...  # two upserts with the same id → both delivered in order
def test_overflow_requests_resync(): ...  # max_queue=10, offer 11 upserts → needs_snapshot, discarded == 11, take(0) == []
def test_resync_returns_current_state(): ...  # after more pumping, resync snapshot bars[-1] == latest SessionStore bar
def test_source_error_degrades_and_recovers(): ...  # fake source raising once → status payload feed_state "degraded" with message, then "ok"
def test_unsubscribe_stops_delivery(): ...  # channel_count() drops to 0; further pump offers nothing to it
```

- [ ] **Step 2: Run** `python -B -m pytest stonx_chart/feed/tests/test_wire.py stonx_chart/feed/tests/test_hub.py -v`. Expected: FAIL (ImportError).
- [ ] **Step 3: Implement `wire.py` and `hub.py`.**
- [ ] **Step 4: Run the same command.** Expected: 15 passed.
- [ ] **Step 5: Commit** `stonx_chart/feed/wire.py stonx_chart/feed/hub.py stonx_chart/feed/tests/test_wire.py stonx_chart/feed/tests/test_hub.py` with message `feat(stonx_chart): add wire encoding and coalescing stream hub`.

---

### Task 6: Loopback HTTP server and CLI

**Files:**
- Create: `stonx_chart/feed/server.py`, `stonx_chart/feed/__main__.py`, `stonx_chart/feed/tests/test_server.py`

**Interfaces:**
- Consumes: `StreamHub`, `ClientChannel` (Task 5); `MockFeedSource`, `SimClock` (Task 2); `wire` (Task 5); `asset_id` (Task 1).
- Produces:
  - `STATIC_DIR = Path(__file__).resolve().parents[1] / "web" / "dist"`
  - `make_server(hub: StreamHub, host: str, port: int, static_dir: Path) -> ThreadingHTTPServer`: raises `ValueError` unless host is `"127.0.0.1"`; `daemon_threads = True`
  - `run_pump(hub: StreamHub, clock: SimClock, stop: threading.Event, period_s: float = 0.05) -> None`: loops `hub.pump(clock.now_ns()); hub.flush_all()` until stop
  - `parse_duration(text: str) -> int` (seconds; accepts `90s`, `60m`, `6h30m`, `0s`; `ValueError` otherwise); `main(argv: list[str] | None = None) -> int`
- **Routes:**
  - `GET /api/stream?asset=SYM`: `Content-Type: text/event-stream`, `Cache-Control: no-cache`. Unknown asset → 400 JSON `{"error":"unknown asset"}`. Sends the snapshot first, then loops `take(0.25)`. When `needs_snapshot` is set, sends `resync()`'s snapshot followed by `status_payload(hub.feed_state, channel.discarded, hub.status_message)`. Sends a heartbeat whenever ≥ 1 s has passed since the last one. Every message is wrapped by one `Envelope(SOURCE_SIMULATED)` per connection. `BrokenPipeError` or `ConnectionResetError` → `unsubscribe` and return.
  - `GET /api/health` → `{"source":"SIMULATED_DEMO","feed_state":…}`.
  - Any other `GET`: static file. `/` serves `index.html`; paths resolving outside `static_dir`, or missing, get 404. Content types are forced for `.html` (`text/html; charset=utf-8`), `.js` (`text/javascript`), `.css` (`text/css`) and `.svg` (`image/svg+xml`). Windows `mimetypes` can map `.js` wrongly.
  - `POST, PUT, PATCH, DELETE, HEAD, OPTIONS` → 405 with `Allow: GET`.
- **CLI flags:** `--seed 7 --speed 1.0 --backfill 60m --history 6h30m --port 8765 --host 127.0.0.1`.
  - A host other than `127.0.0.1` prints an error to stderr and returns 2.
  - Startup: `start_ns` = now minus backfill, floored to a whole second. Build the source and hub (`window_s` = history), run `hub.pump(now)` for the backfill, start `run_pump` with `SimClock(now, speed)` in a thread, print `StonX chart feed (SIMULATED_DEMO) on http://127.0.0.1:{port}/`, then serve until Ctrl+C (return 0).

- [ ] **Step 1: Write the failing tests.** Start the server on port 0 in a thread over a hub pumped 30 s; use `http.client`.

```python
def test_rejects_non_loopback(): pytest.raises(ValueError, make_server, hub, "0.0.0.0", 0, tmp_path); assert main(["--host", "0.0.0.0"]) == 2
def test_non_get_405(): ...  # POST, PUT, DELETE /api/stream → 405 and Allow == "GET"
def test_health(): ...  # 200, {"source": "SIMULATED_DEMO", "feed_state": "ok"}
def test_unknown_asset_400(): ...  # /api/stream?asset=TSLA → 400
def test_stream_snapshot_first_then_heartbeat(): ...  # first data line: type "snapshot", seq 1, asset "NVDA"; a heartbeat arrives within 2 s
def test_disconnect_unsubscribes(): ...  # read the snapshot, close the socket, within 2 s hub.channel_count() == 0
def test_static_files(): ...  # tmp static dir with index.html and app.js: "/" → text/html; "/app.js" → text/javascript; "/../secret.txt" → 404
def test_parse_duration(): assert [parse_duration(x) for x in ("90s","60m","6h30m","0s")] == [90, 3600, 23400, 0]; pytest.raises(ValueError, parse_duration, "5x")
```

- [ ] **Step 2: Run** `python -B -m pytest stonx_chart/feed/tests/test_server.py -v`. Expected: FAIL (ImportError).
- [ ] **Step 3: Implement `server.py` and `__main__.py`.**
- [ ] **Step 4: Run the same command.** Expected: 8 passed.
- [ ] **Step 5: Smoke test by hand.** Run `python -B -m stonx_chart.feed --backfill 5m --port 8765`, then `curl -N "http://127.0.0.1:8765/api/stream?asset=NVDA"` in another shell. Expected: a `data: {"v":1,"type":"snapshot",…}` line, then bar, points and heartbeat lines. Stop both.
- [ ] **Step 6: Commit** `stonx_chart/feed/server.py stonx_chart/feed/__main__.py stonx_chart/feed/tests/test_server.py` with message `feat(stonx_chart): serve the feed over loopback SSE with a CLI`.

---

### Task 7: Golden wire fixtures

**Files:**
- Create: `stonx_chart/feed/fixtures.py`, `stonx_chart/feed/tests/test_fixtures.py`, `stonx_chart/fixtures/wire/nvda_snapshot_300s.json`, `stonx_chart/fixtures/wire/nvda_stream_300_600s.json`, `stonx_chart/fixtures/wire/nvda_snapshot_600s.json`

**Interfaces:**
- Consumes: `StreamHub` and `Envelope` (Task 5), `MockFeedSource` (Task 2).
- Produces:
  - `FIXTURE_SEED = 7`, `FIXTURE_START_MS = 1_793_975_400_000` (2026-11-06 09:30 ET)
  - `build_fixtures() -> dict[str, object]` (filename → JSON value)
  - `write_fixtures(out_dir: Path) -> None`: each file is `json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"`
  - Entry point: `python -B -m stonx_chart.feed.fixtures` writes to `stonx_chart/fixtures/wire/`.
- **Procedure.**
  1. Build a hub over `MockFeedSource(7, start)`. For `s` in 1..300: `pump(start + s·1e9)`, then `flush_all()`.
  2. `subscribe(2)` → `nvda_snapshot_300s.json`: one message wrapped by a new `Envelope` (seq 1).
  3. For `s` in 301..600: pump, `flush_all`, `take(0)`, and wrap each message with the same envelope → `nvda_stream_300_600s.json`, a JSON array whose seq continues from 2.
  4. `nvda_snapshot_600s.json` = `hub.snapshot(2)` wrapped by a fresh `Envelope` (seq 1).
  5. No heartbeats.

- [ ] **Step 1: Write the failing tests**

```python
def test_committed_fixtures_match_regenerated(tmp_path): write_fixtures(tmp_path); for name in FILES: assert (tmp_path/name).read_bytes() == (WIRE_DIR/name).read_bytes()
def test_fixture_sizes(): assert all((WIRE_DIR/n).stat().st_size <= 256 * 1024 for n in FILES)
def test_stream_seq_contiguous(): seqs = [m["seq"] for m in load("nvda_stream_300_600s.json")]; assert seqs == list(range(2, 2 + len(seqs)))
def test_snapshot_600_covers_every_overlay_kind(): ...  # overlay ids include band.fragmented.*, band.high_curl.*, band.holonomy.*, band.halt.*, marker.reset.*, marker.curl.*
```

- [ ] **Step 2: Run** `python -B -m pytest stonx_chart/feed/tests/test_fixtures.py -v`. Expected: FAIL (ImportError).
- [ ] **Step 3: Implement `fixtures.py`, then run `python -B -m stonx_chart.feed.fixtures`.**
- [ ] **Step 4: Run the test command.** Expected: 4 passed. If the coverage test fails, fix the mock schedule or deriver, not the test.
- [ ] **Step 5: Run the whole Python suite:** `python -B -m pytest stonx_chart/feed/tests -q`. Expected: all pass.
- [ ] **Step 6: Commit** `stonx_chart/feed/fixtures.py stonx_chart/feed/tests/test_fixtures.py stonx_chart/fixtures/wire/nvda_snapshot_300s.json stonx_chart/fixtures/wire/nvda_stream_300_600s.json stonx_chart/fixtures/wire/nvda_snapshot_600s.json` with message `test(stonx_chart): add golden wire fixtures`.

---

### Task 8: Web scaffold, protocol types and decoder

**Files:**
- Create: `stonx_chart/web/package.json`, `package-lock.json`, `tsconfig.json`, `vite.config.ts`, `index.html` (placeholder with `<div id="app"></div>` and the module script `/src/main.ts`), `src/main.ts` (placeholder, empty export), `src/protocol/types.ts`, `src/protocol/decode.ts`, `tests/fixtures.ts`, `tests/decode.test.ts`
- Modify: `.gitignore`: append `node_modules/`, `stonx_chart/web/test-results/` and `stonx_chart/web/playwright-report/`

**Interfaces:**
- **package.json scripts:** `dev: vite`, `build: tsc --noEmit && vite build`, `typecheck: tsc --noEmit`, `test: vitest run`, `e2e: vite build && playwright test --grep-invert @perf`, `perf: vite build && playwright test --grep @perf`.
- **Dependencies:** `npm install --save-exact lightweight-charts@5.2.1`; `npm install -D typescript vite vitest @playwright/test @types/node`; `"engines": {"node": ">=22.12"}`.
- **tsconfig:** `strict`, `target ES2022`, `module ESNext`, `moduleResolution bundler`, `lib ["ES2022","DOM"]`, `types ["vite/client","node"]`, include `src`, `tests`, `e2e`.
- **vite.config.ts:** `server.proxy["/api"] = { target: "http://127.0.0.1:8765" }`, `build.outDir = "dist"`, `test.include = ["tests/**/*.test.ts"]`, `test.environment = "node"`.
- **Produces (`types.ts`):**
  - `BaseOverlay {id, kind, group, pane}`
  - `PaneSpec {kind:'pane', title, height_px, order}`
  - `BandSpec {kind:'band', start_ms, end_ms: number|null, color, label}`
  - `MarkerSpec {kind:'marker', time_ms, position:'above'|'below'|'inBar', shape:'arrowUp'|'arrowDown'|'circle'|'square', color, text}`
  - `Agg = 'last'|'mean'|'min'|'max'|'sum'`
  - `SeriesSpec {kind:'series', style:'line'|'area'|'histogram'|'step', color, width, agg: Agg, precision}`
  - `LevelSpec {kind:'level', value, color, label, dash}`
  - `OverlaySpec` (union of the five); `BarCols {t,o,h,l,c,vol: number[]}`; `PointCols {t,v: number[]}`; `Envelope {v:1, source:string, seq:number}`
  - `StreamMessage`: discriminated union on `type`, with the payloads exactly as in Task 5 (`overlay.upsert` has `overlay: OverlaySpec`; `status` has an optional `message`)
- **Produces (`decode.ts`):** `type DecodeResult = {ok:true; message: StreamMessage; ignoredOverlays: number} | {ok:false; reason:'malformed'|'ignored'|'incompatible'; detail:string}` and `decodeMessage(raw: string): DecodeResult`. Rules, checked in this order:
  1. Unparseable, or not an object → malformed.
  2. `v !== 1` → incompatible.
  3. Unknown `type` → ignored.
  4. `overlay.upsert` with an unknown `kind` → ignored.
  5. In a snapshot, unknown-kind overlays are removed and counted in `ignoredOverlays`.
  6. Any wrong field type, out-of-enum value, non-finite number, non-integer time, or columns of unequal length → malformed.
- **Produces (`tests/fixtures.ts`):** `loadFixtureMessages(name: string): string[]`. Reads `../../fixtures/wire/<name>` relative to the test file. A JSON array yields each element re-stringified; an object yields one string.

- [ ] **Step 1: Scaffold.** Create the config files, run the npm installs, and add the `.gitignore` lines. Expected: `npm run typecheck` exits 0.
- [ ] **Step 2: Write the failing tests**

```ts
test('decodes every fixture message', () => { for (const n of ['nvda_snapshot_300s.json','nvda_stream_300_600s.json','nvda_snapshot_600s.json']) for (const raw of loadFixtureMessages(n)) expect(decodeMessage(raw)).toMatchObject({ ok: true, ignoredOverlays: 0 }) })
test('malformed inputs', () => { for (const raw of ['nope', '[]', '{"v":1,"type":"heartbeat","source":"x"}' /* no seq */]) expect(decodeMessage(raw)).toMatchObject({ ok: false, reason: 'malformed' }) })
test('wrong version is incompatible', () => expect(decodeMessage(hb({ v: 2 }))).toMatchObject({ ok: false, reason: 'incompatible' }))
test('unknown type and kind are ignored', () => { expect(decodeMessage(msg({ type: 'wat' }))).toMatchObject({ reason: 'ignored' }); expect(decodeMessage(upsert({ kind: 'cloud' }))).toMatchObject({ reason: 'ignored' }) })
test('snapshot drops unknown overlay kinds', () => { const r = decodeMessage(snapshotWith([{ ...validBand, kind: 'cloud' }, validBand])); expect(r).toMatchObject({ ok: true, ignoredOverlays: 1 }); expect((r as any).message.overlays).toHaveLength(1) })
test('enum and shape violations', () => { expect(decodeMessage(upsert({ ...validMarker, position: 'left' })).ok).toBe(false); expect(decodeMessage(upsert({ ...validSeries, agg: 'median' })).ok).toBe(false); expect(decodeMessage(points({ t: [1000], v: [] })).ok).toBe(false); expect(decodeMessage(snapshotWithBars({ t: [1000], o: [] })).ok).toBe(false) })
test('status message optional', () => expect(decodeMessage(status({ feed_state: 'ok', drop_count: 0 })).ok).toBe(true))
```

(`hb`, `msg`, `upsert`, `points`, `status`, `snapshotWith`, `snapshotWithBars`, `validBand`, `validMarker`, `validSeries` are test-local builders producing JSON strings with a valid envelope.)

- [ ] **Step 3: Run** `npm test -- tests/decode.test.ts`. Expected: FAIL (module not found).
- [ ] **Step 4: Implement `types.ts` and `decode.ts`.**
- [ ] **Step 5: Run the same command.** Expected: 7 passed.
- [ ] **Step 6: Commit** `.gitignore`, all files under `stonx_chart/web/` listed above (including `package-lock.json`) with message `feat(stonx_chart): scaffold web app with wire decoder`.

---

### Task 9: Time buckets and BarStore

**Files:**
- Create: `stonx_chart/web/src/time.ts`, `src/bars/barStore.ts`, `tests/rng.ts`, `tests/barStore.test.ts`

**Interfaces:**
- Consumes: `BarCols` (Task 8).
- Produces:
  - `time.ts`: `type IntervalSec = 1|5|60|300`; `INTERVALS: readonly IntervalSec[] = [1,5,60,300]`; `bucketOf(tMs: number, interval: IntervalSec): number`
  - `barStore.ts`: `interface Bar {t,o,h,l,c,vol: number}`; `interface Whitespace {t: number}`; `type CandleItem = Bar | Whitespace`; `isBar(x: CandleItem): x is Bar`
  - `class BarStore`:
    - `reset(cols: BarCols): void`
    - `applyBar(bar: Bar): 'appended'|'replaced'|'stale'` (equal `t` replaces; older is stale and leaves the store unchanged)
    - `aggregate(interval): Bar[]`: buckets with ≥1 bar; o = first o, c = last c, h = max, l = min, vol = sum; `t` = bucket
    - `bucketBar(interval, bucketMs): Bar | undefined`; `barBuckets(interval): number[]`; `firstMs(): number|null`; `lastMs(): number|null`; `trimBefore(tMs): void`; `get size(): number`
  - `densify(bars: Bar[], interval: IntervalSec, fromMs: number, toMs: number): CandleItem[]`: every bucket from `bucketOf(fromMs)` to `bucketOf(toMs)` inclusive; a bar where present, else whitespace; `[]` when `fromMs > toMs`
  - `tests/rng.ts`: `mulberry32(seed: number): () => number`

- [ ] **Step 1: Write the failing tests**

```ts
const T0 = 1_793_975_400_000
test('bucketOf', () => { expect(bucketOf(T0 + 37_250, 60)).toBe(T0); expect(bucketOf(T0 + 37_250, 5)).toBe(T0 + 35_000); expect(bucketOf(T0 + 37_250, 1)).toBe(T0 + 37_000) })
test('applyBar outcomes', () => { const s = new BarStore(); expect(s.applyBar(bar(T0))).toBe('appended'); expect(s.applyBar(bar(T0, { c: 2 }))).toBe('replaced'); expect(s.applyBar(bar(T0 - 1000))).toBe('stale'); expect(s.size).toBe(1) })
test('aggregates 1s into 5s', () => { /* bars at T0..T0+4000 o=1..5 h=10+i l=0-i c=i vol=1 */ expect(s.aggregate(5)).toEqual([{ t: T0, o: 1, h: 14, l: -4, c: 4, vol: 5 }]) })
test('incremental equals full aggregation (2,000 seeded ops)', () => { /* mulberry32(42): append with gap 1–3 s or replace last; after each op, for each I: s.bucketBar(I, bucketOf(s.lastMs()!, I)) toEqual s.aggregate(I).at(-1) */ })
test('densify fills whitespace inclusively', () => expect(densify([bar(T0), bar(T0 + 3000)], 1, T0, T0 + 3000).map(x => isBar(x) ? 'b' : 'w')).toEqual(['b','w','w','b']))
test('densify empty range', () => expect(densify([], 1, T0 + 1000, T0)).toEqual([]))
test('trimBefore drops older bars', () => { /* bars T0..T0+9000; trimBefore(T0+5000) → firstMs T0+5000, size 5 */ })
test('reset replaces state even with older times', () => { /* applyBar(T0+60_000); reset({t:[T0],…}) → lastMs T0, size 1 */ })
```

- [ ] **Step 2: Run** `npm test -- tests/barStore.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement `time.ts` and `barStore.ts`.** Store 1s bars in growable typed arrays (`Float64Array`, doubling capacity).
- [ ] **Step 4: Run the same command.** Expected: 8 passed.
- [ ] **Step 5: Commit** `stonx_chart/web/src/time.ts stonx_chart/web/src/bars/barStore.ts stonx_chart/web/tests/rng.ts stonx_chart/web/tests/barStore.test.ts` with message `feat(stonx_chart): add 1s bar store with interval aggregation`.

---

### Task 10: Overlay bucketing and OverlayStore

**Files:**
- Create: `stonx_chart/web/src/overlay/bucket.ts`, `src/overlay/overlayStore.ts`, `tests/bucket.test.ts`, `tests/overlayStore.test.ts`

**Interfaces:**
- Consumes: Task 8 types; `IntervalSec` and `bucketOf` (Task 9).
- Produces (`bucket.ts`):
  - `interface Point {t: number; v: number}`
  - `aggregatePoints(cols: PointCols, interval, agg: Agg): Point[]` (`mean` is the arithmetic mean of the 1s values)
  - `bucketValue(cols: PointCols, interval, agg, bucketMs): number | undefined`: scans back from the end, so it's O(points in bucket)
  - `interface BandRender {id, pane, color, label, startMs: number, endMs: number|null}`
  - `bandRender(spec: BandSpec, interval): BandRender`: `startMs = bucketOf(start_ms)`; `endMs = end_ms === null ? null : bucketOf(end_ms - 1)`
  - `interface MarkerRender {id, timeMs, position, shape, color, text}`
  - `placeMarkers(specs: MarkerSpec[], barBuckets: number[], interval): {placed: MarkerRender[]; pending: number}`: snap to the first bar bucket ≥ `bucketOf(time_ms)` by binary search; none → pending. `placed` is sorted by `timeMs`, stable by input order.
- Produces (`overlayStore.ts`): `class OverlayStore`
  - `reset(specs: OverlaySpec[], points: Record<string, PointCols>): {droppedHeld: number}`
  - `upsert(spec): 'applied'|'held'|'ignored'`: held when `pane` isn't `price` or a known pane id. Ignored for a marker whose pane isn't `price`. A pane upsert releases held overlays for that pane.
  - `remove(id): boolean`
  - `appendPoints(id, t: number[], v: number[]): {touched: number[]; rejected: number}`: `t` > last appends; `t` == last replaces; `t` < last is rejected; an unknown series id rejects all
  - `panes(): PaneSpec[]` (sorted by `order`); `series(): SeriesSpec[]`; `levels(): LevelSpec[]`; `bands(interval): BandRender[]`; `markers(): MarkerSpec[]`; `points(id): PointCols|undefined`; `pointsExtent(): {first, last}|null`
  - `groups(): string[]`: distinct groups of applied specs, in first-seen order
  - `trimBefore(tMs): void`: drops points < tMs, bands with `end_ms !== null && end_ms <= tMs`, and markers with `time_ms < tMs`

- [ ] **Step 1: Write the failing tests**

```ts
test('aggregatePoints per agg', () => { const c = { t: [T0, T0+1000, T0+5000], v: [3, 1, 7] }; expect(aggregatePoints(c, 5, 'last')).toEqual([{t:T0,v:1},{t:T0+5000,v:7}]); /* mean→2, min→1, max→3, sum→4 for the first bucket */ })
test('bucketValue matches aggregate for live bucket', () => { /* for each agg: bucketValue(c, 5, agg, T0+5000) === aggregatePoints(c,5,agg).at(-1)!.v */ })
test('bandRender snaps to buckets', () => { expect(bandRender(band(T0+10_500, T0+12_000), 1)).toMatchObject({ startMs: T0+10_000, endMs: T0+11_000 }); expect(bandRender(band(T0+10_500, T0+12_000), 5)).toMatchObject({ startMs: T0+10_000, endMs: T0+10_000 }); expect(bandRender(band(T0, null), 60).endMs).toBeNull() })
test('placeMarkers snapping', () => { const r = placeMarkers([mk('a', T0+1500), mk('b', T0+2500), mk('c', T0+9000)], [T0+1000, T0+4000], 1); expect(r.placed.map(m => [m.id, m.timeMs])).toEqual([['a', T0+1000], ['b', T0+4000]]); expect(r.pending).toBe(1) })
test('same-bucket markers keep arrival order', () => { /* two markers in one bucket → placed ids in input order */ })
test('upsert replaces by id', () => { /* upsert band end null then same id with end → bands(1) has one entry with endMs set */ })
test('held until pane arrives; reset drops held', () => { const s = new OverlayStore(); expect(s.upsert(series('series.x', 'pane.x'))).toBe('held'); expect(s.upsert(pane('pane.x'))).toBe('applied'); expect(s.series().map(x => x.id)).toEqual(['series.x']); s.upsert(series('series.y', 'pane.y')); expect(s.reset([], {}).droppedHeld).toBe(1) })
test('marker off price pane ignored', () => expect(new OverlayStore().upsert({ ...marker, pane: 'pane.curl' })).toBe('ignored'))
test('appendPoints rules', () => { /* append t1, replace t1, reject older, unknown id rejected === t.length */ })
test('groups in first-seen order', () => { /* reset with fixture snapshot 600 s → groups() starts ['lambda2','curl','quotes','vwap'] and contains 'regime','halt','events' */ })
test('trimBefore', () => { /* points, closed band ending before cutoff, marker before cutoff removed; open band kept */ })
```

- [ ] **Step 2: Run** `npm test -- tests/bucket.test.ts tests/overlayStore.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement `bucket.ts` and `overlayStore.ts`.**
- [ ] **Step 4: Run the same command.** Expected: 11 passed.
- [ ] **Step 5: Commit** `stonx_chart/web/src/overlay/bucket.ts stonx_chart/web/src/overlay/overlayStore.ts stonx_chart/web/tests/bucket.test.ts stonx_chart/web/tests/overlayStore.test.ts` with message `feat(stonx_chart): add overlay store with interval bucketing`.

---

### Task 11: ChartSurface contract and ChartController

**Files:**
- Create: `stonx_chart/web/src/surface/types.ts`, `src/surface/fakeSurface.ts`, `src/app/controller.ts`, `tests/controller.test.ts`, `tests/contract.test.ts`

**Interfaces:**
- Consumes: Tasks 8–10.
- Produces (`surface/types.ts`):
  - `interface VisibleRange {fromMs: number; toMs: number}`
  - `interface ChartSurface`:
    - `setCandles(items: CandleItem[])`; `updateCandle(item: CandleItem)` (time ≥ last)
    - `setPanes(panes: PaneSpec[])`; `setSeries(spec: SeriesSpec, data: Point[])` (create or replace); `updateSeriesPoint(id: string, p: Point)`; `removeSeries(id: string)`
    - `setLevels(levels: LevelSpec[])`; `setBands(bands: BandRender[])`; `setMarkers(markers: MarkerRender[])`
    - `getVisibleRange(): VisibleRange|null`; `setVisibleRange(r: VisibleRange)`; `isAtLiveEdge(): boolean`; `scrollToLive()`
    - `setDimmed(d: boolean)`; `onCrosshair(cb: (tMs: number|null) => void)`; `onViewChange(cb: () => void)`; `destroy()`
- Produces (`fakeSurface.ts`): `class FakeSurface implements ChartSurface`
  - `calls: {method: string; args: unknown[]}[]`
  - Model state for assertions: `candles: CandleItem[]`, `series: Map<string, {spec; data: Point[]}>`, `panes`, `levels`, `bands`, `markers`
  - Settable `atLiveEdge = true` and `visibleRange: VisibleRange|null = null`
- Produces (`controller.ts`): `WINDOW_MS = 23_400_000`
  - `interface Counters {malformed; ignored; gaps; stale: number}`
  - `interface Readout {bar: Bar|null; values: {id: string; group: string; value: number}[]}`
  - `interface ControllerState {asset: string|null; assets: string[]; source: string|null; interval: IntervalSec; groups: string[]; hiddenGroups: string[]; feedState: 'ok'|'degraded'; dropCount: number; statusMessage: string|null; lastMessageAtMs: number|null; barCount: number}`
  - `class ChartController`:
    - `constructor(surface: ChartSurface, opts?: {now?: () => number; windowMs?: number; interval?: IntervalSec; hiddenGroups?: string[]})`
    - `connectionOpened()`; `handle(msg: StreamMessage)`; `noteDecodeIssue(reason: 'malformed'|'ignored')`
    - `setInterval(i: IntervalSec)`; `setGroupVisible(group, visible)`; `flush(): {fullReset: boolean}`; `readoutAt(tMs): Readout|null`
    - `readonly counters: Counters`; `state(): ControllerState`
- **Handling (`handle` only mutates stores and dirty state; nothing reaches the surface before `flush`):**
  - Every message sets `lastMessageAtMs`.
  - After `connectionOpened()`, the first message sets the expected seq. Afterward, `seq !== expected` increments `gaps`.
  - `snapshot` resets both stores, adds `droppedHeld` to `ignored`, and marks a full reset.
  - `bar` → `applyBar`; `'stale'` increments `stale`.
  - `series.points` → `appendPoints`; `rejected` is added to `ignored`.
  - An upsert result of `'ignored'` increments `ignored`.
  - `status` sets `feedState`, `dropCount` and `statusMessage`.
  - After bars or points, if the last time minus the first time (across both stores) exceeds `1.1 × windowMs`, both stores `trimBefore(last − windowMs)` and a full reset is marked.
- **Flush:**
  - **Full reset** (snapshot, interval change, trim, visibility toggle):
    1. Remember `wasLive = isAtLiveEdge()` and `range = getVisibleRange()`.
    2. `setCandles(densify(aggregate(I), I, first, last))`, where first and last span the bars and every point.
    3. `setPanes` with visible panes that host at least one visible series; `setSeries` for each visible series; then `setLevels`, `setBands` and `setMarkers` filtered by visible groups.
    4. Then `scrollToLive()` if `wasLive`, else `setVisibleRange(range)` when the range isn't null.
  - **Incremental:**
    1. For each dirty bucket in ascending order beyond the grid's last bucket, emit `updateCandle` whitespace for skipped buckets, then the bucket's bar or whitespace. A dirty bar in the last bucket emits one `updateCandle`. A dirty bar in an older bucket triggers a full reset.
    2. For each dirty `(series, bucket)` in ascending order: `updateSeriesPoint`.
    3. Structural changes re-issue `setPanes`, `setSeries` or `removeSeries`, and `setLevels`.
    4. `setBands` when bands change.
    5. `setMarkers` when markers change, or when a new bar bucket appears while markers are pending.
  - Hidden groups never reach the surface.
  - Call `flush` with nothing dirty → no surface calls.
- `readoutAt`: `bar = bucketBar(I, bucketOf(t))`; values = `bucketValue` for each visible series that has a value there.

- [ ] **Step 1: Write the failing tests** (`controller.test.ts`, with synthetic messages via a local `env(seq)` builder)

```ts
test('snapshot issues one full render', () => { c.handle(snapshot); c.flush(); expect(methods(fake)).toEqual(['isAtLiveEdge','getVisibleRange','setCandles','setPanes', ...seriesCalls, 'setLevels','setBands','setMarkers','scrollToLive']) })  // compare as a multiset for the setSeries part
test('repeated bar updates collapse into one updateCandle', () => { /* 50 bar msgs same t → after flush exactly 1 updateCandle */ })
test('new bucket after gap appends whitespace then bar', () => { /* bars at T0 and T0+3000, interval 1 → updateCandle t T0+1000 (ws), T0+2000 (ws), T0+3000 (bar) */ })
test('halt gap renders whitespace and band', () => { /* snapshot with bars T0..T0+4000; points for series.lambda2 at T0+5000..T0+9000, no bars; halt band start T0+5000 end T0+9500 → fake.candles has whitespace for T0+5000..T0+9000 and fake.bands[0] {startMs: T0+5000, endMs: T0+9000} */ })
test('interval switch re-aggregates and keeps range', () => { fake.atLiveEdge = false; fake.visibleRange = { fromMs: T0, toMs: T0+60_000 }; c.setInterval(5); c.flush(); expect(lastCall(fake, 'setVisibleRange')).toEqual([{ fromMs: T0, toMs: T0+60_000 }]) /* and candles bucketed at 5 s */ })
test('hidden group never reaches the surface', () => { /* hiddenGroups ['quotes'] → no setSeries for series.bid/ask; setGroupVisible('quotes', true) → full reset includes them */ })
test('pending marker appears when its bar arrives', () => { /* marker ahead of bars → setMarkers([]) then bar arrives → setMarkers with it */ })
test('counters', () => { /* seq jump → gaps 1; older bar → stale 1; points for unknown id → ignored 1 */ })
test('burst of messages collapses into bounded surface calls', () => { /* 10,000 bar and points msgs across 10 seconds, one flush → updateCandle calls ≤ 10 and updateSeriesPoint calls ≤ 10 per series */ })
test('trims past 110% of window and keeps live edge', () => { /* windowMs 10_000, 1s bars for 12 s → state().barCount ≤ 11, setCandles issued again, scrollToLive called after it */ })
test('later snapshot with older times replaces state', () => { /* snapshot at T0+600_000, then snapshot at T0 → fake.candles[0].t === T0 and no stale counted */ })
test('readoutAt', () => { /* returns bucket bar and visible series values; hidden series omitted */ })
test('flush with nothing dirty makes no calls', () => { c.flush(); fake.calls.length = 0; c.flush(); expect(fake.calls).toEqual([]) })
```

```ts
// contract.test.ts: cross-language contract via golden fixtures
test.each([1, 5, 60, 300] as const)('incremental stream equals snapshot at 600 s (interval %i)', (I) => {
  const a = run(['nvda_snapshot_600s.json'], I), b = run(['nvda_snapshot_300s.json', 'nvda_stream_300_600s.json'], I)
  expect(b.fake.candles).toEqual(a.fake.candles); expect([...b.fake.series]).toEqual([...a.fake.series])
  expect(b.fake.bands).toEqual(a.fake.bands); expect(b.fake.markers).toEqual(a.fake.markers); expect(b.fake.levels).toEqual(a.fake.levels)
  expect(b.controller.counters).toEqual({ malformed: 0, ignored: 0, gaps: 0, stale: 0 })
})  // run(): decode each message, handle it, flush after every 20 messages and at the end
```

- [ ] **Step 2: Run** `npm test -- tests/controller.test.ts tests/contract.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement `types.ts`, `fakeSurface.ts` and `controller.ts`.**
- [ ] **Step 4: Run the same command.** Expected: 17 passed.
- [ ] **Step 5: Commit** `stonx_chart/web/src/surface/types.ts stonx_chart/web/src/surface/fakeSurface.ts stonx_chart/web/src/app/controller.ts stonx_chart/web/tests/controller.test.ts stonx_chart/web/tests/contract.test.ts` with message `feat(stonx_chart): add chart controller and surface contract`.

---

### Task 12: StreamClient

**Files:**
- Create: `stonx_chart/web/src/transport/streamClient.ts`, `tests/streamClient.test.ts`

**Interfaces:**
- Consumes: `decodeMessage`, `StreamMessage` (Task 8).
- Produces:
  - `type ConnState = 'connecting'|'live'|'stale'|'reconnecting'|'incompatible'|'closed'`
  - `interface EventSourceLike {onmessage: ((e: {data: string}) => void)|null; onerror: (() => void)|null; onopen: (() => void)|null; close(): void}`
  - `interface StreamClientOptions {url: (asset: string) => string; onMessage: (m: StreamMessage, ignoredOverlays: number) => void; onDecodeIssue: (r: 'malformed'|'ignored') => void; onState: (s: ConnState) => void; onConnection: () => void; createEventSource?: (url: string) => EventSourceLike; staleAfterMs?: number}`
  - `class StreamClient {constructor(o: StreamClientOptions); open(asset: string): void; close(): void; readonly state: ConnState}`
- **Behavior:**
  - `open` cancels timers, closes the current source, bumps a generation counter, creates a new source, calls `onConnection()` and sets `connecting`. Handlers from older generations do nothing.
  - Every message restarts the 3,000 ms stale timer.
  - A snapshot sets `live` and resets the backoff to 500.
  - Stale timer expiry: set `stale`, close, then schedule a reconnect. `onerror`: close, then schedule a reconnect. Scheduling sets `reconnecting`, waits `backoff` and reopens the same asset; backoff becomes `min(backoff*2, 5000)`.
  - `incompatible` closes the source with no reconnect. `close()` sets `closed` and clears timers.

- [ ] **Step 1: Write the failing tests** (fake event sources plus `vi.useFakeTimers()`)

```ts
test('opens, goes live on snapshot', () => { c.open('NVDA'); expect(urls).toEqual(['/api/stream?asset=NVDA']); expect(c.state).toBe('connecting'); last().onmessage!({ data: SNAPSHOT }); expect(c.state).toBe('live'); expect(onConnection).toHaveBeenCalledTimes(1) })
test('stale after 3 s then backoff 500, 1000 … capped at 5000', () => { /* advance 3000 → 'stale' then 'reconnecting'; advance 500 → 2nd source; repeat → delays 1000, 2000, 4000, 5000, 5000 */ })
test('backoff resets after snapshot', () => { /* after two failures, snapshot, failure → next delay 500 */ })
test('onerror reconnects', () => { /* last().onerror!() → source closed, new source after 500 ms */ })
test('incompatible version stops reconnecting', () => { /* v:2 message → state 'incompatible'; advance 60_000 → no new source */ })
test('malformed reported, connection kept', () => { /* 'nope' → onDecodeIssue('malformed'), state unchanged */ })
test('asset switch cancels pending reconnect and ignores old stream', () => { /* NVDA source errors (reconnect pending), open('AMD'), advance 10_000 → only AMD sources created; old NVDA handler fires a snapshot → onMessage not called */ })
```

- [ ] **Step 2: Run** `npm test -- tests/streamClient.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement `streamClient.ts`.**
- [ ] **Step 4: Run the same command.** Expected: 7 passed.
- [ ] **Step 5: Commit** `stonx_chart/web/src/transport/streamClient.ts stonx_chart/web/tests/streamClient.test.ts` with message `feat(stonx_chart): add reconnecting SSE stream client`.

---

### Task 13: Lightweight Charts surface and theme

**Files:**
- Create: `stonx_chart/web/src/surface/theme.ts`, `src/surface/paneLayout.ts`, `src/surface/lwcSurface.ts`, `src/surface/lwcBands.ts`, `tests/theme.test.ts`, `tests/paneLayout.test.ts`

**Interfaces:**
- Consumes: `ChartSurface` and render types (Tasks 10–11).
- Produces:
  - `theme.ts`: `type Scheme = 'light'|'dark'`; `TOKENS: readonly string[]` (the 11 global tokens); `resolveColor(token: string, scheme: Scheme, alpha = 1): string` (returns `rgba(r, g, b, a)`; unknown tokens resolve as `neutral`); `chartColors(scheme): {background, text, grid, up, down}`
  - `paneLayout.ts`: `paneIndexMap(panes: PaneSpec[], hosted: Set<string>): Map<string, number>` (`price` → 0; hosted panes sorted by `order` → 1..n; unhosted panes absent)
  - `lwcSurface.ts`: `interface LwcSurface extends ChartSurface {setScheme(s: Scheme): void}` and `createLwcSurface(container: HTMLElement, o: {scheme: Scheme; formatTime: (tMs: number) => string; formatTick: (tMs: number) => string}): LwcSurface`
  - `lwcBands.ts`: `class BandsPrimitive implements IPanePrimitive`, with `setBands(bands: BandRender[], scheme: Scheme)`
- **LwcSurface decisions:**
  - `createChart` options: `autoSize: true`, `layout.attributionLogo: true`, `timeScale: {timeVisible: true, secondsVisible: true, shiftVisibleRangeOnNewBar: true, tickMarkFormatter}`, `localization.timeFormatter`.
  - Times: `(tMs / 1000) as UTCTimestamp`.
  - Candles use `CandlestickSeries` on pane 0.
  - Series styles: `line` → `LineSeries`; `step` → `LineSeries` with `lineType: LineType.WithSteps`; `area` → `AreaSeries`; `histogram` → `HistogramSeries`. `priceFormat: {type: 'price', precision, minMove: 10 ** -precision}`.
  - Pane placement comes from `paneIndexMap`. When the map changes, call `moveToPane` for affected series in ascending target order, then `chart.panes()[i].setHeight(height_px)`.
  - Levels are `createPriceLine` on the pane's first series, all recreated on `setLevels`.
  - Markers: one `createSeriesMarkers(candles, [])` handle. `above`/`below`/`inBar` map to `aboveBar`/`belowBar`/`inBar`.
  - Bands: one `BandsPrimitive` per pane index, attached with `chart.panes()[i].attachPrimitive`, using `zOrder: 'bottom'` and alpha 0.18. x runs from `timeToCoordinate(start) − barSpacing/2` to `timeToCoordinate(end) + barSpacing/2`, or the pane's right edge when `endMs` is null. The band label is drawn at its top-left. Primitives are re-attached when pane indices change.
  - `isAtLiveEdge`: `timeScale().scrollPosition() > -1`. `scrollToLive`: `timeScale().scrollToRealTime()`. Visible range goes through `timeScale().getVisibleRange()` and `setVisibleRange`.
  - `onViewChange` → `subscribeVisibleLogicalRangeChange`. `onCrosshair` → `subscribeCrosshairMove`; a missing `param.time` gives null.
  - `setDimmed` sets the container's opacity to 0.5. `destroy` → `chart.remove()`.
  - Resolve every API name against `node_modules/lightweight-charts/dist/typings.d.ts`.

- [ ] **Step 1: Write the failing tests**

```ts
test('every token resolves in both schemes and differs from neutral', () => { for (const t of TOKENS.filter(t => t !== 'neutral')) for (const s of ['light','dark'] as const) expect(resolveColor(t, s)).not.toBe(resolveColor('neutral', s)) })
test('unknown token falls back to neutral', () => expect(resolveColor('nope', 'dark')).toBe(resolveColor('neutral', 'dark')))
test('alpha applied', () => expect(resolveColor('state.halt', 'light', 0.18)).toMatch(/^rgba\(\d+, \d+, \d+, 0\.18\)$/))
test('paneIndexMap orders hosted panes', () => expect([...paneIndexMap([pane('b', 2), pane('a', 1), pane('c', 3)], new Set(['b', 'c']))]).toEqual([['price', 0], ['b', 1], ['c', 2]]))
```

- [ ] **Step 2: Run** `npm test -- tests/theme.test.ts tests/paneLayout.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement all four files.**
- [ ] **Step 4: Run the same command, then `npm run typecheck`.** Expected: 4 passed; typecheck exits 0, confirming the library API names exist.
- [ ] **Step 5: Commit** `stonx_chart/web/src/surface/theme.ts stonx_chart/web/src/surface/paneLayout.ts stonx_chart/web/src/surface/lwcSurface.ts stonx_chart/web/src/surface/lwcBands.ts stonx_chart/web/tests/theme.test.ts stonx_chart/web/tests/paneLayout.test.ts` with message `feat(stonx_chart): render through Lightweight Charts surface`.

---

### Task 14: UI shell and wiring

**Files:**
- Create: `stonx_chart/web/src/ui/format.ts`, `src/ui/prefs.ts`, `src/ui/controls.ts`, `src/ui/legend.ts`, `src/ui/readout.ts`, `src/ui/statusBar.ts`, `src/ui/banners.ts`, `src/ui/styles.css`, `tests/format.test.ts`, `tests/prefs.test.ts`
- Modify: `stonx_chart/web/index.html`, `stonx_chart/web/src/main.ts` (replace the placeholders)

**Interfaces:**
- Consumes: `ChartController` (Task 11), `StreamClient` (Task 12), `createLwcSurface` (Task 13).
- Produces:
  - `format.ts`: `DISPLAY_TZ = 'America/New_York'`; `formatTime(tMs, withSeconds: boolean): string` (24-hour `HH:mm[:ss]`); `formatTick(tMs, interval): string` (seconds shown when interval < 60); `formatAge(ms: number|null): string` (`—`, `0.4 s`, `12 s`, `3 min`); `formatNumber(v, precision): string`
  - `prefs.ts`: `interface Prefs {asset: string; interval: IntervalSec; hiddenGroups: string[]}`; `PREFS_KEY = 'stonx_chart.prefs.v1'`; `DEFAULT_PREFS = {asset: 'NVDA', interval: 60, hiddenGroups: []}`; `safeStorage(): Storage|null`; `loadPrefs(s: Pick<Storage,'getItem'>|null): Prefs` (any throw or invalid field → defaults for that field); `savePrefs(s: Pick<Storage,'setItem'>|null, p: Prefs): void` (swallows errors)
  - Each UI module exports `create<Name>(root: HTMLElement, handlers)` returning `{render(state: ControllerState, conn: ConnState): void}`. DOM test hooks:
    - Chart container: `data-testid="chart"`, `data-bar-count`, `data-interval`, `data-asset`. These are set by `render` after the flush that applied them.
    - `data-testid="source-banner"` (text `SIMULATED DEMO DATA — not market data`, shown when `source === 'SIMULATED_DEMO'`, not dismissable)
    - `data-testid="stale-banner"` (shown for `stale` and `reconnecting`)
    - `data-testid="incompatible-banner"`, `data-testid="degraded-banner"` (shows `statusMessage`)
    - `data-testid="legend-item"` with `data-group` and `aria-pressed`
    - `data-testid="asset-select"`, `data-testid="interval-select"`, `data-testid="jump-live"`, `data-testid="conn-state"`
- **`main.ts` wiring:**
  - Load prefs and create the surface. Scheme comes from `matchMedia('(prefers-color-scheme: dark)')`, and its change event calls `setScheme`.
  - Create the controller with the prefs' interval and hidden groups, and the `StreamClient` with `url = a => '/api/stream?asset=' + encodeURIComponent(a)`. Its callbacks: `onMessage(m, n)` → `controller.handle(m)` plus `noteDecodeIssue('ignored')` `n` times; `onDecodeIssue` → `controller.noteDecodeIssue`; `onConnection` → `controller.connectionOpened()`.
  - `requestAnimationFrame` loop: `controller.flush()`, then render every UI module.
  - A 1 s `setInterval` re-renders the status bar's age.
  - Asset select → `client.open` and save prefs. Interval select → `controller.setInterval` and save. Legend click → `setGroupVisible` and save.
  - Jump-to-live → `surface.scrollToLive()`; it's hidden while `surface.isAtLiveEdge()` (rechecked on `onViewChange` and each frame). Crosshair → readout.
  - Connection state `stale` or `reconnecting` → `surface.setDimmed(true)`, else false.
  - The asset list is populated from the snapshot's `assets`.

- [ ] **Step 1: Write the failing tests**

```ts
test('formats in New York time across DST', () => { expect(formatTime(1_793_975_400_000, true)).toBe('09:30:00'); expect(formatTime(1_782_912_600_000, true)).toBe('09:30:00'); expect(formatTime(1_793_975_400_000, false)).toBe('09:30') })
test('formatAge', () => { expect(formatAge(null)).toBe('—'); expect(formatAge(400)).toBe('0.4 s'); expect(formatAge(12_000)).toBe('12 s'); expect(formatAge(180_000)).toBe('3 min') })
test('loadPrefs falls back on throwing storage', () => expect(loadPrefs({ getItem: () => { throw new Error('blocked') } })).toEqual(DEFAULT_PREFS))
test('loadPrefs falls back on bad data', () => { expect(loadPrefs(store('not json'))).toEqual(DEFAULT_PREFS); expect(loadPrefs(store(JSON.stringify({ asset: 'AMD', interval: 7 })))).toEqual({ ...DEFAULT_PREFS, asset: 'AMD' }) })
test('prefs round trip and setItem errors swallowed', () => { /* save then load equals; savePrefs with throwing setItem does not throw; savePrefs(null, p) does not throw */ })
```

- [ ] **Step 2: Run** `npm test -- tests/format.test.ts tests/prefs.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement the UI modules, `index.html` and `main.ts`.**
- [ ] **Step 4: Run the same command, then `npm run build`.** Expected: 5 passed; build writes `dist/index.html`.
- [ ] **Step 5: Manual check.** Run `python -B -m stonx_chart.feed --backfill 10m --speed 5`, open `http://127.0.0.1:8765/`, and confirm:
  - Candles, the λ₂ and star-curl panes, the demo banner and legend toggles all appear.
  - Switching interval and asset works.
  - Stopping the feed shows the stale banner within ~3 s.
- [ ] **Step 6: Commit** the files listed above with message `feat(stonx_chart): add chart UI shell and wiring`.

---

### Task 15: End-to-end and performance tests

**Files:**
- Create: `stonx_chart/web/playwright.config.ts`, `e2e/feed.ts`, `e2e/smoke.spec.ts`, `e2e/perf.spec.ts`

**Interfaces:**
- Consumes: the CLI (Task 6) and the built app (Task 14).
- Produces: `startFeed(o: {port: number; speed: number; backfill: string; history?: string}): Promise<{url: string; stop(): Promise<void>}>`. It spawns `${process.env.STONX_PYTHON ?? 'python'} -B -m stonx_chart.feed --seed 7 …` with cwd at the repo root (`path.resolve(__dirname, '../../..')`), then polls `/api/health` every 100 ms for up to 60 s.
- **Playwright config:** Chromium only, `testDir: 'e2e'`, `timeout: 60_000`, no `webServer` (tests start feeds themselves), `workers: 1`.

- [ ] **Step 1: Write the tests**

```ts
// smoke.spec.ts: feed on port 8799, speed 20, backfill '10m'
test('renders candles, demo banner and legend', async ({ page }) => { /* goto; expect data-bar-count > 0; source-banner visible; legend data-group set ⊇ ['regime','halt','events','lambda2','curl','quotes','vwap'] */ })
test('switches interval and asset', async ({ page }) => { /* select 5 → data-interval '5' and bar count > the count at 60; select AMD → data-asset 'AMD' and bar count > 0 */ })
test('recovers after server restart', async ({ page }) => { /* stop → stale-banner visible within 5 s; startFeed same port → stale-banner hidden within 10 s; data-asset and data-interval unchanged; bar count > 0 */ })
test('legend toggle persists across reload', async ({ page }) => { /* click lambda2 → aria-pressed false; reload → still false */ })
// perf.spec.ts (@perf): speed 1, backfill '6h30m', history '6h30m', test timeout 300_000
test('interval switch < 100 ms, asset switch < 500 ms, pan p95 frame ≤ 20 ms @perf', async ({ page }) => { /* in page.evaluate: t0 = performance.now(); change select; MutationObserver until data-interval (or data-asset + data-bar-count > 0) matches; return elapsed. Pan: drag across the chart for 2 s while collecting rAF deltas; assert p95 ≤ 20 */ })
test('new-bar message reaches the screen within 50 ms p95 @perf', async ({ page }) => { /* page.addInitScript wraps window.EventSource to stamp performance.now() on each message whose type is "bar" with a new t; a MutationObserver on data-bar-count stamps the change; over 30 new bars, p95(change − receipt) ≤ 50. The server's 50 ms slot flush completes the spec's 100 ms budget */ })
```

- [ ] **Step 2: Run** `npx playwright install chromium`, then `npm run e2e`. Locally, first set `$env:STONX_PYTHON` to the absolute path of the repo's `.venv\Scripts\python.exe`. Expected: 4 passed.
- [ ] **Step 3: Run `npm run perf`.** Expected: 2 passed. If a threshold fails, profile and fix the flush or reset path. Record the measured numbers in the commit message.
- [ ] **Step 4: Commit** `stonx_chart/web/playwright.config.ts stonx_chart/web/e2e/feed.ts stonx_chart/web/e2e/smoke.spec.ts stonx_chart/web/e2e/perf.spec.ts` with message `test(stonx_chart): add end-to-end smoke and performance checks`.

---

### Task 16: CI, notices and docs

**Files:**
- Modify: `.github/workflows/ci.yml` (add a job), `.github/dependabot.yml` (add an npm entry), `THIRD_PARTY_NOTICES.md` (add a section), `docs/superpowers/specs/2026-11-06-stonx-chart-overlay-engine-design.md` (§2 toolchain row: "Node 22 LTS or newer")
- Create: `stonx_chart/README.md`

- [ ] **Step 1: Add the `stonx_chart` CI job.**
  - Matrix: `[ubuntu-latest, windows-latest]`; `timeout-minutes: 20`.
  - Reuse the existing pinned `actions/checkout` and `actions/setup-python` SHAs (Python `3.12`).
  - Pin `actions/setup-node` to the full commit SHA of its latest release tag (look it up with `gh api repos/actions/setup-node/git/ref/tags/<tag>`), with a `# vX.Y.Z` comment; `node-version: 22`; `cache: npm`; `cache-dependency-path: stonx_chart/web/package-lock.json`.
  - Steps:
    1. `python -m pip install --disable-pip-version-check -r requirements-dev.txt`
    2. `python -B -m pytest stonx_chart/feed/tests -q -p no:cacheprovider`
    3. In `stonx_chart/web`: `npm ci`, `npm run typecheck`, `npm test`, `npx playwright install --with-deps chromium`, then `npm run e2e` with `STONX_PYTHON: python`
- [ ] **Step 2: Dependabot.** Add `package-ecosystem: npm`, `directory: /stonx_chart/web`, matching the existing entries' schedule.
- [ ] **Step 3: Notices.** Add a "Bundled dependencies" section to `THIRD_PARTY_NOTICES.md`: lightweight-charts `5.2.1`, Apache-2.0, with the copyright line copied from `stonx_chart/web/node_modules/lightweight-charts/LICENSE`. Note that the attribution logo stays enabled (`layout.attributionLogo: true`).
- [ ] **Step 4: Write `stonx_chart/README.md`.** Cover what it is (read-only, `SIMULATED_DEMO` mock), feed commands and flags, dev mode (`npm run dev` with the feed on 8765), production (`npm run build`, then the feed serves `http://127.0.0.1:8765/`), and the test commands. Use relative links only.
- [ ] **Step 5: Full verification:**
  - `python -B -m pytest stonx_chart/feed/tests -q` → all pass.
  - In `stonx_chart/web`: `npm run typecheck && npm test && npm run e2e` → all pass.
  - `git status --short`: the only new or modified paths are this plan's files, alongside the pre-existing unrelated deletions.
- [ ] **Step 6: Commit** `.github/workflows/ci.yml .github/dependabot.yml THIRD_PARTY_NOTICES.md stonx_chart/README.md docs/superpowers/specs/2026-11-06-stonx-chart-overlay-engine-design.md` with message `ci(stonx_chart): add chart CI job, notices and docs`.
