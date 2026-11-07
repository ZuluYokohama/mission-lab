import { expect, test, type Page } from "@playwright/test";
import { startFeed, type Feed } from "./feed";

// Spec section 12 budgets: interval switch < 100 ms, asset switch < 500 ms, pan p95 frame <= 20 ms,
// new bar on screen within 50 ms p95 of the message (the server's 50 ms slot flush is the other half).
// "On screen" means applied and painted: every measurement ends two animation frames after the DOM
// attribute that reports the change (the attributes are written in the flush's frame, the chart draws
// in the next one).
const PORT = 8798;
const URL = `http://127.0.0.1:${PORT}/`;

let feed: Feed | null = null;

// Not serial: a failing budget must not hide the other test. (Playwright restarts the worker after a
// failure, which starts a fresh feed for the next test; that costs about a minute but only when one fails.)

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

/** Changes a select in the page and resolves with the ms until the chart container reports `done`, applied and painted. */
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
          obs.disconnect();
          clearTimeout(timer);
          // Applied (the attribute) and painted (the chart draws in the frame after the flush's).
          requestAnimationFrame(() => requestAnimationFrame(() => resolve(performance.now() - t0)));
        });
        obs.observe(chart, { attributes: true });
        const t0 = performance.now();
        select.value = v;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }),
    [testid, value, done] as const,
  );
}

/**
 * Drags the chart back and forth for 2 s, one pointer move per animation frame (as a real pointer
 * delivers them; a burst of moves with no frame in between is not a drag a user can produce), while
 * sampling requestAnimationFrame deltas. Asserts the drag really panned (the "Jump to live" button
 * appears once the view leaves the live edge) and that enough frames were sampled, then returns the
 * frame-time summary and jumps back to the live edge.
 */
async function pan(page: Page): Promise<{ frames: number; p95: number; max: number }> {
  const jump = page.getByTestId("jump-live");
  await expect(jump).toBeHidden(); // starts at the live edge
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
  let panned = false;
  const start = Date.now();
  while (Date.now() - start < 2_000) {
    const phase = ((Date.now() - start) % 1_000) / 1_000; // 0..1 per sweep
    const x = box.x + box.width * (0.2 + 0.6 * (phase < 0.5 ? phase * 2 : 2 - phase * 2));
    await page.mouse.move(x, y);
    // Resolves on the next frame; also reports whether the view has left the live edge.
    const away = await page.evaluate(
      () => new Promise<boolean>((r) => requestAnimationFrame(() => r(!(document.querySelector('[data-testid="jump-live"]') as HTMLElement).hidden))),
    );
    panned ||= away;
  }
  await page.mouse.up();
  const frames = await page.evaluate(() => {
    const w = window as unknown as { __frames: number[]; __stop: boolean };
    w.__stop = true;
    return w.__frames.slice(1);
  });
  expect(panned, "the drag moved the view away from the live edge").toBe(true);
  expect(frames.length).toBeGreaterThan(30);
  if (await jump.isVisible()) await jump.click();
  await expect(jump).toBeHidden();
  return { frames: frames.length, p95: percentile(frames, 0.95), max: Math.max(...frames) };
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
    // Stamp two frames after the attribute write: applied and painted.
    new MutationObserver(() => {
      requestAnimationFrame(() => requestAnimationFrame(() => w.__shown.push(performance.now())));
    }).observe(chart, { attributes: true, attributeFilter: ["data-bar-count"] });
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
  expect(lags.length).toBeGreaterThanOrEqual(25);
  const p95 = percentile(lags, 0.95);
  const summary = { bars: lags.length, p95, max: Math.max(...lags), median: percentile(lags, 0.5) };
  console.log("PERF " + JSON.stringify(summary));
  test.info().annotations.push({ type: "perf", description: JSON.stringify(summary) });

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

  // Pan at the default 60 s grid (left from the switches above) and at the dense 1 s grid.
  const pan60 = await pan(page);
  await page.getByTestId("interval-select").selectOption("1");
  await expect(page.getByTestId("chart")).toHaveAttribute("data-interval", "1");
  await page.waitForTimeout(500);
  const pan1 = await pan(page);

  const summary = { intervals, assets, pan60, pan1 };
  console.log("PERF " + JSON.stringify(summary));
  test.info().annotations.push({ type: "perf", description: JSON.stringify(summary) });

  // Soft, so one run reports all three budgets.
  expect.soft(worstInterval, `interval switch ms ${JSON.stringify(intervals)}`).toBeLessThan(100);
  expect.soft(worstAsset, `asset switch ms ${JSON.stringify(assets)}`).toBeLessThan(500);
  expect.soft(pan60.p95, "pan p95 frame ms at 60 s").toBeLessThanOrEqual(20);
  expect.soft(pan1.p95, "pan p95 frame ms at 1 s").toBeLessThanOrEqual(20);
});
