import { expect, test, type Locator, type Page } from "@playwright/test";
import { startFeed, type Feed } from "./feed";

const PORT = 8799;
const URL = `http://127.0.0.1:${PORT}/`;

let feed: Feed | null = null;

test.describe.configure({ mode: "serial" });

test.beforeAll(async () => {
  feed = await startFeed({ port: PORT, speed: 20, backfill: "10m" });
});

test.afterAll(async () => {
  await feed?.stop();
  feed = null;
});

const chartOf = (page: Page): Locator => page.getByTestId("chart");
const barCount = async (page: Page): Promise<number> => Number((await chartOf(page).getAttribute("data-bar-count")) ?? "0");

async function open(page: Page): Promise<void> {
  await page.goto(URL);
  await expect.poll(() => barCount(page)).toBeGreaterThan(0);
}

async function setInterval(page: Page, value: "1" | "5" | "60" | "300"): Promise<void> {
  await page.getByTestId("interval-select").selectOption(value);
  await expect(chartOf(page)).toHaveAttribute("data-interval", value);
}

test("renders candles, demo banner and legend", async ({ page }) => {
  await open(page);
  await expect(page.getByTestId("source-banner")).toBeVisible();
  await expect.poll(async () => (await page.getByTestId("legend-item").evaluateAll((els) => els.map((e) => (e as HTMLElement).dataset.group))).sort()).toEqual(
    expect.arrayContaining(["curl", "events", "halt", "lambda2", "quotes", "regime", "vwap"]),
  );
});

test("switches interval and asset", async ({ page }) => {
  await open(page);
  await setInterval(page, "60");
  const at60 = await barCount(page);
  await page.getByTestId("interval-select").selectOption("5");
  await expect(chartOf(page)).toHaveAttribute("data-interval", "5");
  await expect.poll(() => barCount(page)).toBeGreaterThan(at60);

  await page.getByTestId("asset-select").selectOption("AMD");
  await expect(chartOf(page)).toHaveAttribute("data-asset", "AMD");
  await expect.poll(() => barCount(page)).toBeGreaterThan(0);
});

test("recovers after server restart", async ({ page }) => {
  await open(page);
  await setInterval(page, "5");
  await page.getByTestId("asset-select").selectOption("AMD");
  await expect(chartOf(page)).toHaveAttribute("data-asset", "AMD");
  const lambda2 = page.getByTestId("legend-item").and(page.locator('[data-group="lambda2"]'));
  await expect(lambda2).toHaveAttribute("aria-pressed", "true");
  await lambda2.click();
  await expect(lambda2).toHaveAttribute("aria-pressed", "false");

  await feed!.stop();
  await expect(page.getByTestId("stale-banner")).toBeVisible({ timeout: 5_000 });

  feed = await startFeed({ port: PORT, speed: 20, backfill: "10m" });
  await expect(page.getByTestId("stale-banner")).toBeHidden({ timeout: 10_000 });
  await expect(chartOf(page)).toHaveAttribute("data-asset", "AMD");
  await expect(chartOf(page)).toHaveAttribute("data-interval", "5");
  await expect.poll(() => barCount(page)).toBeGreaterThan(0);
  // The hidden group stays hidden across the reconnect's fresh snapshot.
  await expect(lambda2).toHaveAttribute("aria-pressed", "false");
});

test("legend toggle persists across reload", async ({ page }) => {
  await open(page);
  const item = page.getByTestId("legend-item").and(page.locator('[data-group="lambda2"]'));
  await expect(item).toHaveAttribute("aria-pressed", "true");
  await item.click();
  await expect(item).toHaveAttribute("aria-pressed", "false");
  await page.reload();
  await expect(item).toHaveAttribute("aria-pressed", "false");
});

test("hiding lambda2 keeps the curl pane drawing", async ({ page }) => {
  await open(page);
  await setInterval(page, "1");
  await expect(chartOf(page)).toHaveAttribute("data-pane-count", "2");

  const item = page.getByTestId("legend-item").and(page.locator('[data-group="lambda2"]'));
  await item.click();
  await expect(item).toHaveAttribute("aria-pressed", "false");
  await expect(chartOf(page)).toHaveAttribute("data-pane-count", "1");
  const before = await barCount(page);
  await expect.poll(() => barCount(page), { timeout: 5_000 }).toBeGreaterThan(before);
  await expect(chartOf(page)).toHaveAttribute("data-pane-count", "1");

  await item.click();
  await expect(item).toHaveAttribute("aria-pressed", "true");
  await expect(chartOf(page)).toHaveAttribute("data-pane-count", "2");
});

test("follows the live edge while streaming", async ({ page }) => {
  await open(page);
  await setInterval(page, "1");
  const jump = page.getByTestId("jump-live");
  await expect(jump).toBeHidden();
  const start = await barCount(page);
  const deadline = Date.now() + 3_000;
  while (Date.now() < deadline) {
    await expect(jump).toBeHidden();
    await page.waitForTimeout(100);
  }
  expect(await barCount(page)).toBeGreaterThan(start);
  await expect(jump).toBeHidden();
});

test("jump to live returns to the live edge", async ({ page }) => {
  await open(page);
  await setInterval(page, "1");
  const jump = page.getByTestId("jump-live");
  await expect(jump).toBeHidden();

  // Drag the chart to the right: the view moves back into history.
  const box = (await chartOf(page).boundingBox())!;
  const y = box.y + box.height * 0.3;
  await page.mouse.move(box.x + box.width * 0.3, y);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.7, y, { steps: 10 });
  await page.mouse.up();
  await expect(jump).toBeVisible();

  // While in history, the view stays put as bars keep arriving.
  const before = await barCount(page);
  await expect.poll(() => barCount(page), { timeout: 5_000 }).toBeGreaterThan(before);
  await expect(jump).toBeVisible();

  await jump.click();
  await expect(jump).toBeHidden();
  const after = await barCount(page);
  await expect.poll(() => barCount(page), { timeout: 5_000 }).toBeGreaterThan(after);
  await expect(jump).toBeHidden();
  await page.waitForTimeout(1_000);
  await expect(jump).toBeHidden();
});
