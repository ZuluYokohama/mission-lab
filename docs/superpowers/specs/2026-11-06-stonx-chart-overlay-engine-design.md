# StonX Chart & Overlay Engine — Design

**Date:** 2026-11-06
**Status:** Implemented in PR 15, pending maintainer review. The section 12 criteria 1–3 are not yet met in `npm run perf`.
**Location:** new top-level directory `stonx_chart/`

## 1. Goal

A browser price chart for live monitoring of one StonX focus asset, with a declarative overlay engine that draws engine signals on and around the price. v1 runs against a deterministic mock feed whose records match the StonX `TickEntry` and `SheafSignalEntry` contracts, so a real engine source can replace it without changing anything downstream.

## 2. Decisions

| Topic | Decision |
|---|---|
| Base chart | Candles for one focus asset `v*` (one of 12), chosen with a selector |
| Resolution | OHLC bars at 1s / 5s / 1m / 5m, live last bar, pan and zoom across the retained history |
| Overlay kinds | Regime bands, event markers, indicator sub-panes, on-price series, horizontal levels |
| Overlay source | Declarative: the Python server streams overlay specs as data; the frontend renders any valid spec without code changes |
| Data source (v1) | `MockFeedSource`, contract-shaped and deterministic; every message labeled `SIMULATED_DEMO` |
| Primary use | Live monitoring during market hours (live edge first; no replay mode in v1) |
| Renderer | TradingView Lightweight Charts 5.2.x behind a `ChartSurface` adapter |
| Toolchain | TypeScript + Vite + Vitest + Playwright (Node 22 LTS or newer); Python 3.12 standard library for the server |
| Transport | Server-sent events, snapshot on every connect |

## 3. Non-goals (v1)

- Order entry, order routing, or any write path to a broker. The chart is read-only.
- Session replay, scrubbing, or recorded-file sources.
- Client-side indicator plugins (for example moving averages computed in the browser).
- User drawing tools.
- Multi-asset layouts.
- OLS beta panes and advisory verdict markers. The spec supports both, and the deriver can add them later in Python alone.
- Reading StonX shared memory (`ShmFeedSource`). The `FeedSource` interface is designed for it; the implementation comes later.
- Repairing the existing CI jobs that target the deleted `research/` tree.

## 4. Context

The StonX engine described in `STONX_EQUITIES_ENGINE_PLAN.md` and `PROJECT.md` is not present in this checkout: `research/` is deleted in the working tree. The relevant contracts are taken from those documents:

- `TickEntry` (64 bytes): `epoch_ns`, `price`, `size_scaled`, `tick_type` (1 BID, 2 ASK, 4 LAST, 5 VOLUME), `flags` (0x1 TierA, 0x2 TierB, 0x4 Halt, 0x8 Error317), `asset_id`, `sequence`, `ask_price`, `ask_size_scaled`.
- `SheafSignalEntry` (2,048 bytes): `epoch_ns`, `sequence`, `fiedler_lambda`, `regime_flags` (0x1 Fragmented, 0x2 High Curl, 0x4 Holonomy Spike), `fiedler_vec[12]`, `alpha_coexact[72]`, `b_harm[72]`, `ols_betas[90]`.
- Asset order: 0 XLK, 1 SOXX, 2 NVDA, 3 QQQ, 4 AMD, 5 SPY, 6 AAPL, 7 MSFT, 8 AMZN, 9 META, 10 GOOGL, 11 AVGO.

The engine's 18-edge list is not available. `records.py` defines a **provisional** `EDGES` list of 18 edges over the 12 assets, consistent with the documented neighbors of NVDA (XLK, SOXX, AMD, AVGO). It must be replaced with the engine's `EDGES` when `ShmFeedSource` is built. `alpha_coexact` is assumed edge-major (`index = edge * 4 + stalk`); this must be confirmed against the engine at the same time.

The earlier workbench prototype (`.agents/teamwork/explorer_m8_3/proposed_reality_workbench.html`) established the loopback-only server and SSE pattern reused here.

## 5. Architecture

```
stonx_chart/
├── __init__.py
├── feed/                      # Python 3.12, standard library only at runtime
│   ├── __main__.py            # python -m stonx_chart.feed --seed 7 --speed 1 --backfill 60m --port 8765
│   ├── records.py             # TickRecord, SignalFrame, ASSETS, provisional EDGES
│   ├── sources.py             # FeedSource protocol + MockFeedSource
│   ├── session.py             # SessionStore: ticks -> 1s bars per asset, rolling history
│   ├── derive.py              # OverlayDeriver: frames + tick flags -> overlay ops
│   ├── wire.py                # message encoding (schema v1), ns -> ms conversion
│   ├── server.py              # loopback HTTP: static web/dist, GET /api/stream, GET /api/health
│   └── tests/
├── web/
│   ├── package.json, package-lock.json, vite.config.ts, tsconfig.json
│   ├── src/
│   │   ├── protocol/          # wire types + decoder (trust boundary)
│   │   ├── transport/         # StreamClient: connect, heartbeat watch, reconnect
│   │   ├── bars/              # BarStore: 1s bars -> interval aggregation
│   │   ├── overlay/           # OverlayStore: specs, series points, bucketing, surface ops
│   │   ├── surface/           # ChartSurface interface, LwcSurface, FakeSurface (tests)
│   │   ├── ui/                # pickers, legend, crosshair readout, banners, status bar
│   │   └── main.ts            # wiring + requestAnimationFrame flush loop
│   └── tests/                 # Vitest unit tests; e2e/ for Playwright
└── fixtures/wire/             # golden wire messages written by Python, read by TS tests
```

### 5.1 Data flow

1. `FeedSource` yields `TickRecord`s and `SignalFrame`s in time order.
2. `SessionStore` folds ticks into 1-second bars for all 12 assets and keeps a rolling window (default 23,400 s, one regular session) in `array` columns.
3. `OverlayDeriver` consumes frames and tick flags and emits overlay operations, downsampled to 1-second series points.
4. The server streams one asset per connection: a `snapshot`, then incremental messages.
5. The browser stores everything at 1-second resolution and re-buckets candles and every overlay to the active interval. Interval changes need no server request, and every overlay aligns with the candle time grid.

### 5.2 Principles

- **1-second base resolution on the wire.** Raw ticks and per-tick signal frames never reach the browser.
- **Snapshot on every connect.** Reconnects and server-side resyncs rebuild client state from scratch. There is no partial resume.
- **`FeedSource` is the only swap seam.** Nothing downstream of it knows whether data is mock or live.
- **Read-only by construction.** The server answers GET only and exposes no command endpoints. Every message carries `source`. The UI shows a non-dismissable banner when `source` is `SIMULATED_DEMO`.

## 6. Wire protocol (v1)

Each SSE event's data is one JSON object:

```json
{"v": 1, "type": "<type>", "source": "SIMULATED_DEMO", "seq": 42, "...": "..."}
```

- All times are **integer milliseconds since the Unix epoch**. Nanosecond values exceed JavaScript's exact integer range, so the server converts.
- `seq` increases by exactly 1 per message within a connection. The client counts gaps, which should never occur.
- Bar and interval buckets are aligned to UTC epoch multiples of the interval.

| `type` | Payload | Notes |
|---|---|---|
| `snapshot` | `asset`, `assets` (12 × `{id, symbol}`), `session_start_ms`, `bars` (columnar `t, o, h, l, c, vol`), `overlays` (all live specs), `points` (`{series_id: {t: [], v: []}}`) | Always the first message on a connection |
| `bar` | `t, o, h, l, c, vol` | Upsert by `t`. The live bar repeats its `t` |
| `overlay.upsert` | one overlay spec | A spec with an existing `id` replaces it entirely |
| `overlay.remove` | `id` | |
| `series.points` | `id`, `points` (`{t: [], v: []}`) | Appends. The final `t` may equal the last stored `t` to update the live point |
| `heartbeat` | `now_ms` | Every 1 s |
| `status` | `feed_state` (`ok` or `degraded`), `drop_count`, optional `message` | Sent on change |

Payload keys never reuse the envelope keys `v`, `type`, `source` or `seq`.

The server coalesces live-bar and live-point updates into latest-value slots flushed every 50 ms (at most 20 per second). Overlay upserts and removes are delivered in order without coalescing.

## 7. Overlay spec

Every overlay has `id` (unique), `kind`, `group` (legend toggle key) and `pane` (`"price"` or the `id` of a `pane` overlay).

Colors are semantic tokens. The theme maps each to light and dark values, and unknown tokens render as `neutral`. v1 tokens: `regime.fragmented`, `regime.high_curl`, `regime.holonomy`, `state.halt`, `event.reset`, `event.curl_surge`, `series.primary`, `series.secondary`, `series.quote`, `level.threshold`, `neutral`.

| `kind` | Fields | Rendering |
|---|---|---|
| `pane` | `title`, `height_px`, `order` | Chart sub-pane sharing the time axis; `order` sets top-to-bottom position below the price pane. The pane appears when its first series is added |
| `band` | `start_ms`, `end_ms` (exclusive; `null` while open), `color`, `label` | Pane primitive drawn behind the series in its pane (`zOrder: "bottom"`); an open band extends to the live edge |
| `marker` | `time_ms`, `position` (`above`, `below`, `inBar`), `shape` (`arrowUp`, `arrowDown`, `circle`, `square`), `color`, `text` | `createSeriesMarkers` on the candle series. v1 requires `pane: "price"`; markers for other panes are ignored and counted |
| `series` | `style` (`line`, `area`, `histogram`, `step`), `color`, `width`, `agg` (`last`, `mean`, `min`, `max`, `sum`), `precision` | Library series in its pane; data comes from `series.points` |
| `level` | `value`, `color`, `label`, `dash` | Price line on the pane's first series (the candles for `price`); held until that pane has a series |

### 7.1 Bucketing rules

For an interval `I`, the bucket of time `t` is `floor(t / I) * I`.

- **Series:** the points in a bucket combine with the spec's `agg` into one value at the bucket time.
- **Bands:** a band covers every bucket that intersects `[start_ms, end_ms)`. A band whose end falls inside a bucket still shades that whole bucket.
- **Markers:** a marker goes on its bucket's bar. If that bucket has no bar, it moves to the next bucket that has one. If no later bar exists yet, the marker waits until one arrives. Multiple markers in one bucket stack in arrival order.

### 7.2 v1 deriver output for focus asset `v*`

| Group | Overlay | Source |
|---|---|---|
| `regime` | Bands `FRAGMENTED`, `HIGH_CURL`, `HOLONOMY_SPIKE` | Rising edge of `regime_flags` bit opens a band; falling edge closes it |
| `halt` | Band `HALT` | `TickEntry.flags & 0x4` on `v*` |
| `events` | Marker "Book reset" | `TickEntry.flags & 0x8` on `v*` |
| `events` | Marker "Curl surge `<A>–<B>`" | For an edge `e` touching `v*`, let `n(e)` = ‖α_coexact(e)‖₂ over its 4 stalk values. A surge fires when `(n(e) − μ(e)) / σ(e) > 2.5`, where μ and σ are the rolling 5-minute mean and standard deviation of `n(e)`. Surges need 60 s of warm-up and have a 30 s per-edge cooldown |
| `lambda2` | Pane "λ₂", series (`line`, `agg: last`), levels at 0.05 and 0.15 | `fiedler_lambda` |
| `curl` | Pane "Star curl", series (`area`, `agg: max`) | ‖α_coexact restricted to edges touching `v*`‖₂ |
| `quotes` | Series bid and ask on `price` (`line`, `agg: last`, `series.quote`) | Last bid and ask per second |
| `vwap` | Series VWAP on `price` (`line`, `agg: last`) | VWAP of LAST trades since `session_start_ms` (for the mock, the start of backfilled history) |

Overlay text never contains trade-direction instructions such as BUY or SELL.

## 8. Client engine

- **`protocol/decode`** validates raw JSON into typed messages or returns a `DecodeError`. It is the only shape check; downstream code trusts its output.
- **`BarStore`** holds 1s bars in growable typed arrays. `aggregate(I)` returns all bars for an interval. `applyBar(bar)` updates the matching 1s bar, recomputes only the affected interval bucket and returns it. Invariant: the result after any sequence of `applyBar` calls equals `aggregate(I)` over the same data. When retained bars exceed the window by 10%, the oldest are trimmed and the surface gets a single full reset.
- **`OverlayStore`** holds specs by `id` and series points at 1s. For the active interval it computes bucketed series values, band bucket ranges and snapped marker positions. It tracks what changed since the last flush and emits surface ops: `addPane`, `removePane`, `upsertSeries`, `setSeriesData`, `updateSeriesPoint`, `setBands`, `setMarkers`, `upsertLevel`, `remove`.
- **`ChartSurface`** is the rendering interface. `LwcSurface` implements it with Lightweight Charts and is the only module that imports the library. `FakeSurface` records ops for tests.
- **Flush loop:** messages only mutate stores. One `requestAnimationFrame` loop applies pending changes to the surface at most once per frame.
- **Live-edge follow:** the view tracks the live edge until the user pans or zooms away. Then a "Jump to live" control appears and the view holds position while data arrives.
- **Interval switch:** re-aggregate from the 1s stores, replace all surface data and keep the visible time range.
- **Asset switch:** close the stream, open `/api/stream?asset=<symbol>`, and reset on the new snapshot.
- **UI:**
  - Asset picker (12) and interval picker (1s, 5s, 1m, 5m).
  - Legend with one toggle per `group`.
  - Crosshair readout: OHLC, volume and the value of every visible overlay at the hovered bucket.
  - `SIMULATED_DEMO` banner.
  - Status bar: connection state, age of last message, malformed, ignored and gap counts, server `drop_count`.
  - Times display in America/New_York via `Intl.DateTimeFormat`; timestamps are never shifted.
  - Legend toggles and the last asset and interval persist in `localStorage`. Every access is wrapped in try/catch, and the page works without storage.
- **Licensing:** `lightweight-charts` is Apache-2.0. Its attribution logo stays enabled, and an entry is added to `THIRD_PARTY_NOTICES.md`.

## 9. Mock feed and server

- **`MockFeedSource(seed, start_ns)`** is deterministic for a given seed, start and platform. `SimClock` owns the speed. Prices and signal values are rounded to reduce last-bit math-library differences between platforms. That does not guarantee byte-identical output across platforms, and CI checks the golden fixtures on ubuntu and windows.
  - **Prices:** each asset's return = market factor + sector factor (index ETFs, semis, megacap tech) + idiosyncratic noise. Bid/ask spreads of a few basis points. At least one LAST trade per asset per second.
  - **Signals:** frames at 10 Hz. `fiedler_lambda` is a mean-reverting walk with occasional dips below 0.05. `alpha_coexact` is 72 values with occasional single-edge surges. `ols_betas` and `fiedler_vec` are filled with plausible bounded values. `regime_flags` follows the values: Fragmented (0x1) while `fiedler_lambda < 0.05`, High Curl (0x2) while total ‖α_coexact‖₂ exceeds a fixed mock threshold.
  - **Holonomy:** `b_harm` is all zeros, matching the documented `dim H¹ = 0` structure, except during rare scheduled "corruption" windows. In those windows `b_harm` carries non-zero energy and Holonomy Spike (0x4) is set, so the `HOLONOMY_SPIKE` band can be exercised.
  - **Scheduled events:** halts and Error 317 resets at seed-determined times.
  - It matches the data's shape only. It does not implement sheaf math and is never presented as engine output.
- **CLI:** `--seed`, `--speed` (1 = wall clock), `--backfill` (history to pre-generate at start), `--port` (default 8765), `--history` (rolling window, default 6h30m).
- **Server:** `ThreadingHTTPServer` bound to `127.0.0.1`. Any other bind address exits with an error. Routes:
  - `GET /api/stream?asset=<symbol>`: SSE.
  - `GET /api/health`: JSON with `source` and `feed_state`.
  - `GET /*`: static files from `web/dist`.
  - Any other method: 405.
- **Fan-out:** each client has its own queue (at most 2,000 messages) and coalescing slots. The source loop never blocks on a client.
- **Development:** Vite's dev server proxies `/api` to the Python server.

## 10. Error handling

| Failure | Behavior |
|---|---|
| Malformed message | Drop, count, show count in status bar, continue |
| Unknown `type` or `kind` | Ignore and count (forward compatibility) |
| `v` ≠ 1 | Blocking error banner; close the stream |
| `bar` older than the last bar | Ignore and count |
| Overlay referencing an unknown pane | Hold until the pane spec arrives; a new snapshot discards held overlays |
| `seq` gap | Count; no recovery action |
| No message for 3 s (heartbeats are 1 s) | `STALE` banner, dim the chart, reconnect with backoff 0.5 s doubling to a 5 s cap; the new snapshot replaces all state |
| Client queue over 2,000 messages | Server clears that queue and sends a fresh `snapshot` |
| Source exception | `status` with `feed_state: degraded` and a message; heartbeats continue |
| Non-loopback bind | Refuse at startup with non-zero exit |
| `Host` header other than `127.0.0.1` or `localhost` (DNS rebinding) | 421 |
| Non-GET request | 405 |

## 11. Testing

**Python (pytest from `requirements-dev.txt`; runtime stays standard-library only):**
- Mock determinism: same seed gives an identical message sequence.
- Bar building: OHLC from LAST ticks, volume, rolling-window eviction.
- Deriver: flag edges open and close bands; curl-surge threshold, warm-up and cooldown; λ₂ and star-curl series values; no BUY or SELL text in any overlay.
- Server: refuses non-loopback binds; returns 405 for non-GET; `snapshot` is always first; slow-client resync; heartbeat cadence.

**Golden wire fixtures:** a fixed seed over a 10-minute session writes `stonx_chart/fixtures/wire/*.json`. A Python test checks the committed files match a fresh run. TS tests decode the same files and assert the resulting store state. This is the cross-language contract test. Each fixture file stays under 256 KiB.

**TypeScript (Vitest):**
- Decoder on fixtures and on malformed, unknown-kind and wrong-version inputs.
- `BarStore` property test: incremental updates equal full re-aggregation at every interval.
- `OverlayStore` bucketing for each `agg`, band coverage and the marker-snapping rules, including markers that arrive ahead of any bar.
- Flush-loop coalescing: many messages in one frame give one surface flush.
- Engine-to-surface ops verified with `FakeSurface`.

**End-to-end (Playwright, Chromium):** start the mock server at high `--speed`, load the page, and check:
- Candles render and the `SIMULATED_DEMO` banner is visible.
- The legend lists `regime`, `halt`, `events`, `lambda2`, `curl`, `quotes`, `vwap`.
- Switching interval and asset works.
- After the server is stopped and restarted, the page reconnects on its own, keeps its asset, interval and legend toggles, and renders the new snapshot.

**CI:** a new `stonx_chart` job on ubuntu-latest and windows-latest runs `python -B -m pytest stonx_chart/feed/tests`, `npm ci`, Vitest, `vite build` and Playwright. `node_modules/` is added to `.gitignore` (`dist/` is already ignored).

## 12. Success criteria

Current status: criteria 1–3 are not yet met in `npm run perf`. It misses the new-bar-to-paint p95, pan p95 and interval-switch budgets. The asset-switch budget passes.

1. A tick reaches the screen within 100 ms over loopback (at most 50 ms of server coalescing plus one frame).
2. Pan and zoom hold 60 fps with 23,400 1-second bars and all v1 overlays visible.
3. Switching interval on a full window takes under 100 ms; switching asset takes under 500 ms from click to rendered snapshot.
4. After the server restarts, the page reconnects without a reload and keeps its asset, interval and legend toggles. A restarted mock is a new data stream, so the data itself changes.
5. Every view of mock data shows the `SIMULATED_DEMO` label.

## 13. Follow-ups after v1

- `ShmFeedSource` reading `stonx_shm_ingestion` and `stonx_shm_signals` read-only, replacing the provisional `EDGES` and confirming the `alpha_coexact` layout.
- OLS beta pane with an edge picker.
- Advisory verdict markers using the existing vocabulary (`UNCALIBRATED`, `INCONCLUSIVE`, `QUALIFIED_POSITIVE_LIFT`, `QUALIFIED_NEGATIVE_LIFT`).
- Session replay mode on the same `FeedSource` interface.
