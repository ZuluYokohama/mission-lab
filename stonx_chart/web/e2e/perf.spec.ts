import { expect, test, type Page } from "@playwright/test";
import { startFeed, type Feed } from "./feed";

// Spec section 12 budgets: interval switch < 100 ms, asset switch < 500 ms, pan p95 frame <= 20 ms,
// new bar on screen within 50 ms p95 of the message (the server's 50 ms slot flush is the other half).
const PORT = 8798;
const URL = `http://127.0.0.1:${PORT}/`;

let feed: Feed | null = null;

test.describe.configure({ mode: "serial" });

test.beforeAll(async () => {
  test.setTimeout(300_000);
  feed = await startFeed({ port: PORT, speed: 1, backfill: "6h30m", history: "6h30m", healthTimeoutMs: 240_000 });
});

test.afterAll(async () => {
  await feed?.stop();
  feed = null;
});

const percentile = (xs: number[], p: number): number => {
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.ceil(p * s.length) - 1)]!;
};

async function loaded(page: Page): Promise<void> {
  await page.goto(URL);
  await expect(page.getByTestId("chart")).toHaveAttribute("data-asset", "NVDA", { timeout: 60_000 });
  await expect.poll(async () => Number((await page.getByTestId("chart").getAttribute("data-bar-count")) ?? "0"), { timeout: 60_000 }).toBeGreaterThan(0);
}

/** Changes a select in the page and resolves with the ms until the chart container reports `done`. */
function timedChange(page: Page, testid: string, value: string, done: "interval" | "asset"): Promise<number> {
  return page.evaluate(
    ([id, v, kind]) =>
      new Promise<number>((resolve, reject) => {
        const chart = document.querySelector('[data-testid="chart"]') as HTMLElement;
        const select = document.querySelector(`[data-testid="${id}"]`) as HTMLSelectElement;
        const ok = (): boolean =>
          kind === "interval"
            ? chart.dataset.interval === v
            : chart.dataset.asset === v && Number(chart.dataset.barCount ?? "0") > 0;
        const timer = setTimeout(() => {
          obs.disconnect();
          reject(new Error(`timed out waiting for ${kind}=${v}`));
        }, 20_000);
        const obs = new MutationObserver(() => {
          if (!ok()) return;
          const elapsed = performance.now() - t0;
          obs.disconnect();
          clearTimeout(timer);
          resolve(elapsed);
        });
        obs.observe(chart, { attributes: true });
        const t0 = performance.now();
        select.value = v;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }),
    [testid, value, done] as const,
  );
}

test("new-bar message reaches the screen within 50 ms p95 @perf", async ({ page }) => {
  test.setTimeout(300_000);
  await page.addInitScript(() => {
    const w = window as unknown as { __recv: number[]; __shown: number[] };
    w.__recv = [];
    w.__shown = [];
    const Native = window.EventSource;
    let lastT: number | null = null;
    class Stamped extends Native {
      constructor(url: string | URL, init?: EventSourceInit) {
        super(url, init);
        lastT = null;
        this.addEventListener("message", (e) => {
          const data = (e as MessageEvent<string>).data;
          // Cheap pre-check; only bar messages are parsed.
          if (!data.includes('"bar"')) return;
          try {
            const m = JSON.parse(data) as { type?: string; t?: number };
            if (m.type === "bar" && typeof m.t === "number" && m.t !== lastT) {
              if (lastT !== null) w.__recv.push(performance.now());
              lastT = m.t;
            }
          } catch {
            // not JSON: ignore
          }
        });
      }
    }
    window.EventSource = Stamped as typeof EventSource;
  });
  await loaded(page);
  // At 1 s bars every new bar changes the bar count.
  await page.getByTestId("interval-select").selectOption("1");
  await expect(page.getByTestId("chart")).toHaveAttribute("data-interval", "1");
  await page.waitForTimeout(1_000);

  await page.evaluate(() => {
    const w = window as unknown as { __recv: number[]; __shown: number[] };
    w.__recv.length = 0;
    w.__shown.length = 0;
    const chart = document.querySelector('[data-testid="chart"]') as HTMLElement;
    new MutationObserver(() => w.__shown.push(performance.now())).observe(chart, { attributes: true, attributeFilter: ["data-bar-count"] });
  });

  const w = (): Promise<number> => page.evaluate(() => (window as unknown as { __recv: number[] }).__recv.length);
  await expect.poll(w, { timeout: 90_000, intervals: [500] }).toBeGreaterThanOrEqual(30);

  const { recv, shown } = await page.evaluate(() => {
    const x = window as unknown as { __recv: number[]; __shown: number[] };
    return { recv: x.__recv, shown: x.__shown };
  });
  const lags: number[] = [];
  for (const r of recv.slice(0, 30)) {
    const s = shown.find((t) => t >= r);
    if (s !== undefined) lags.push(s - r);
  }
  const p95 = percentile(lags, 0.95);
  const summary = { bars: lags.length, p95, max: Math.max(...lags), median: percentile(lags, 0.5) };
  console.log("PERF " + JSON.stringify(summary));
  test.info().annotations.push({ type: "perf", description: JSON.stringify(summary) });

  expect(lags.length).toBeGreaterThanOrEqual(25);
  expect(p95).toBeLessThanOrEqual(50);
});

test("interval switch < 100 ms, asset switch < 500 ms, pan p95 frame <= 20 ms @perf", async ({ page }) => {
  test.setTimeout(300_000);
  await loaded(page);

  // Interval switches over the full 6.5 h history, including the densest (1 s) grid.
  const intervals: Record<string, number> = {};
  for (const v of ["5", "1", "300", "60"]) {
    intervals[v] = await timedChange(page, "interval-select", v, "interval");
    await page.waitForTimeout(300);
  }
  const worstInterval = Math.max(...Object.values(intervals));

  const assets: Record<string, number> = {};
  for (const v of ["AMD", "NVDA"]) {
    assets[v] = await timedChange(page, "asset-select", v, "asset");
    await page.waitForTimeout(500);
  }
  const worstAsset = Math.max(...Object.values(assets));

  // Pan: drag back and forth across the chart for 2 s while sampling requestAnimationFrame deltas.
  await page.evaluate(() => {
    const w = window as unknown as { __frames: number[]; __stop: boolean };
    w.__frames = [];
    w.__stop = false;
    let last = performance.now();
    const tick = (t: number): void => {
      w.__frames.push(t - last);
      last = t;
      if (!w.__stop) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  const box = (await page.getByTestId("chart").boundingBox())!;
  const y = box.y + box.height * 0.3;
  await page.mouse.move(box.x + box.width * 0.2, y);
  await page.mouse.down();
  // One pointer move per animation frame, as a real pointer delivers them (a burst of moves with no
  // frame in between is not a drag a user can produce): sweep left/right across the chart for 2 s.
  const start = Date.now();
  while (Date.now() - start < 2_000) {
    const phase = ((Date.now() - start) % 1_000) / 1_000; // 0..1 per sweep
    const x = box.x + box.width * (0.2 + 0.6 * (phase < 0.5 ? phase * 2 : 2 - phase * 2));
    await page.mouse.move(x, y);
    await page.evaluate(() => new Promise<void>((r) => requestAnimationFrame(() => r())));
  }
  await page.mouse.up();
  const frames = await page.evaluate(() => {
    const w = window as unknown as { __frames: number[]; __stop: boolean };
    w.__stop = true;
    return w.__frames.slice(1);
  });
  const p95 = percentile(frames, 0.95);

  const summary = { intervals, assets, pan: { frames: frames.length, p95, max: Math.max(...frames) } };
  console.log("PERF " + JSON.stringify(summary));
  test.info().annotations.push({ type: "perf", description: JSON.stringify(summary) });

  // Soft, so one run reports all three budgets. The serial feed is shared, so this test runs last: a
  // failure here would skip any test after it.
  expect(frames.length).toBeGreaterThan(30);
  expect.soft(worstInterval, `interval switch ms ${JSON.stringify(intervals)}`).toBeLessThan(100);
  expect.soft(worstAsset, `asset switch ms ${JSON.stringify(assets)}`).toBeLessThan(500);
  expect.soft(p95, "pan p95 frame ms").toBeLessThanOrEqual(20);
});
