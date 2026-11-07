import { ChartController } from "./app/controller";
import { createLwcSurface } from "./surface/lwcSurface";
import type { Scheme } from "./surface/theme";
import type { IntervalSec } from "./time";
import { StreamClient, type ConnState } from "./transport/streamClient";
import { createBanners } from "./ui/banners";
import { createControls } from "./ui/controls";
import { formatTick, formatTime } from "./ui/format";
import { createLegend } from "./ui/legend";
import { loadPrefs, safeStorage, savePrefs, type Prefs } from "./ui/prefs";
import { createReadout } from "./ui/readout";
import { createStatusBar } from "./ui/statusBar";
import "./ui/styles.css";

function element(id: string): HTMLElement {
  const el = document.getElementById(id);
  if (el === null) throw new Error(`missing #${id}`);
  return el;
}

const storage = safeStorage();
const prefs: Prefs = loadPrefs(storage);
const persist = (): void => savePrefs(storage, prefs);

const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
const schemeOf = (dark: boolean): Scheme => (dark ? "dark" : "light");

const chartEl = element("chart");
let tickInterval: IntervalSec = prefs.interval;
const surface = createLwcSurface(chartEl, {
  scheme: schemeOf(darkQuery.matches),
  formatTime: (t) => formatTime(t, true),
  formatTick: (t) => formatTick(t, tickInterval),
});
darkQuery.addEventListener("change", (e) => surface.setScheme(schemeOf(e.matches)));

const controller = new ChartController(surface, { interval: prefs.interval, hiddenGroups: prefs.hiddenGroups });

let conn: ConnState = "connecting";
const client = new StreamClient({
  url: (a) => "/api/stream?asset=" + encodeURIComponent(a),
  onMessage: (m, ignoredOverlays) => {
    controller.handle(m);
    for (let i = 0; i < ignoredOverlays; i++) controller.noteDecodeIssue("ignored");
  },
  onDecodeIssue: (r) => controller.noteDecodeIssue(r),
  onState: (s) => {
    conn = s;
    surface.setDimmed(s === "stale" || s === "reconnecting");
  },
  onConnection: () => controller.connectionOpened(),
});

const controls = createControls(element("controls"), {
  initialAsset: prefs.asset,
  onAsset: (asset) => {
    prefs.asset = asset;
    persist();
    client.open(asset);
  },
  onInterval: (interval) => {
    tickInterval = interval;
    controller.setInterval(interval);
    prefs.interval = interval;
    persist();
  },
  onJumpLive: () => surface.scrollToLive(),
  isAtLiveEdge: () => surface.isAtLiveEdge(),
});
const legend = createLegend(element("legend"), {
  onToggle: (group, visible) => {
    controller.setGroupVisible(group, visible);
    prefs.hiddenGroups = controller.state().hiddenGroups;
    persist();
  },
});
const readout = createReadout(element("readout"), { readoutAt: (t) => controller.readoutAt(t) });
const statusBar = createStatusBar(element("status"), { now: Date.now });
const banners = createBanners(element("banners"));

/** Test hooks on the chart container; set after the flush that applied them. */
function renderChartAttrs(asset: string | null, interval: IntervalSec, barCount: number): void {
  const set = (name: string, value: string): void => {
    if (chartEl.getAttribute(name) !== value) chartEl.setAttribute(name, value);
  };
  set("data-bar-count", String(barCount));
  set("data-interval", String(interval));
  set("data-asset", asset ?? "");
}

function renderAll(): void {
  const state = controller.state();
  renderChartAttrs(state.asset, state.interval, state.barCount);
  controls.render(state, conn);
  legend.render(state, conn);
  readout.render(state, conn);
  statusBar.render(state, conn);
  banners.render(state, conn);
}

surface.onCrosshair((t) => readout.setCrosshair(t));
surface.onViewChange(() => controls.render(controller.state(), conn));

function frame(): void {
  controller.flush();
  renderAll();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
window.setInterval(() => statusBar.render(controller.state(), conn), 1000);
window.addEventListener("pagehide", () => client.close());
window.addEventListener("pageshow", (e) => {
  if (e.persisted) client.open(prefs.asset); // restored from the back/forward cache
});

client.open(prefs.asset);
