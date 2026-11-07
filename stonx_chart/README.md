# StonX chart

A live candlestick chart with a declarative overlay engine. A Python feed streams bars and overlay specs as data; a TypeScript page renders them with [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts) behind a `ChartSurface` adapter. The design is in [the overlay engine spec](../docs/superpowers/specs/2026-11-06-stonx-chart-overlay-engine-design.md).

## What this is, and is not

- **The data is simulated.** The only data source is a deterministic mock, and every message it sends is labeled `SIMULATED_DEMO`. Nothing here is market data, and nothing on the chart is a signal, a quote or advice.
- **The server is loopback-only.** It binds `127.0.0.1` and refuses any other `--host`.
- **The chart is read-only.** The server answers `GET` only (every other method gets `405`), and the page has no order entry of any kind.
- **The attribution logo stays on.** Lightweight Charts is Apache-2.0; its logo is kept enabled and its notice is in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Requirements

- Python 3.12. The feed uses only the standard library.
- Node 22.12 or newer (CI uses Node 22) for the web app and its tests.

## Run the feed

From the repository root:

```
python -m stonx_chart.feed
```

| Flag | Default | Meaning |
|---|---|---|
| `--seed` | `7` | Seed of the mock feed; the same seed gives the same bars. |
| `--speed` | `1` | Simulated seconds per real second. |
| `--backfill` | `60m` | History generated at start. |
| `--history` | `6h30m` | Rolling window kept in memory. |
| `--port` | `8765` | Port to listen on. |
| `--host` | `127.0.0.1` | Must be `127.0.0.1`; any other value is an error. |

Endpoints: `GET /api/stream?asset=SYM` (server-sent events: a snapshot on connect, then live updates), `GET /api/health`, and `GET /*` for the built web app.

## Develop the web app

Start the feed on its default port, then the Vite dev server:

```
python -m stonx_chart.feed
cd stonx_chart/web
npm ci
npm run dev
```

Vite proxies `/api` to the feed on `127.0.0.1:8765`, so the feed must be on port 8765 in dev mode.

## Production build

```
cd stonx_chart/web
npm ci
npm run build
cd ../..
python -m stonx_chart.feed
```

`npm run build` type-checks and writes `stonx_chart/web/dist`. The feed serves that directory, so the whole app is then at <http://127.0.0.1:8765/>.

## Tests

From the repository root:

```
python -B -m pytest stonx_chart/feed/tests -q -p no:cacheprovider
```

In `stonx_chart/web`:

| Command | What it runs |
|---|---|
| `npm run typecheck` | `tsc --noEmit`. |
| `npm test` | Vitest unit tests. |
| `npm run e2e` | Builds, then runs the Playwright smoke checks in Chromium against a real feed. |
| `npm run perf` | Builds, then runs the Playwright performance targets. Not part of CI. |

The first e2e run needs a browser: `npx playwright install chromium`. The e2e tests start the feed themselves, using `python` from the path or the interpreter named by the `STONX_PYTHON` environment variable.

CI runs the feed tests, `typecheck`, `test` and `e2e` on Ubuntu and Windows.

### Known and pending: performance targets

`npm run perf` currently misses three of its four targets: interval switch, pan smoothness and new-bar-to-paint latency. This is known and the work is pending, so a failing `npm run perf` is not a regression and `perf` is deliberately kept out of CI. Do not read the chart as meeting its performance targets until this is closed.
