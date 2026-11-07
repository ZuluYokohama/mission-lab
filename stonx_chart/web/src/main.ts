import { ChartController } from "./app/controller";
import { createErrorReporter, runFrame } from "./app/frameLoop";
import { createLwcSurface } from "./surface/lwcSurface";
import type { Scheme } from "./surface/theme";
import type { IntervalSec } from "./time";
import { streamRejected } from "./transport/assetProbe";
import { StreamClient, type ConnState } from "./transport/streamClient";
import { createBanners } from "./ui/banners";
import { createControls } from "./ui/controls";
import { formatTick, formatTime } from "./ui/format";
import { createLegend } from "./ui/legend";
import { DEFAULT_PREFS, loadPrefs, safeStorage, savePrefs, type Prefs } from "./ui/prefs";
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
const streamUrl = (a: string): string => "/api/stream?asset=" + encodeURIComponent(a);

// A stored asset the feed does not know is refused (HTTP 400) forever. If the connection fails before
// any snapshot has ever arrived, ask the feed whether it refuses that asset and, if so, fall back to
// the default asset (once) and save it. A feed that is merely down is not a refusal.
let snapshotSeen = false;
let fellBack = false;
let probing = false;
function maybeFallBack(): void {
  const stored = prefs.asset;
  if (snapshotSeen || fellBack || probing || stored === DEFAULT_PREFS.asset) return;
  probing = true;
  void streamRejected(streamUrl(stored)).then((rejected) => {
    probing = false;
    if (!rejected || snapshotSeen || fellBack || prefs.asset !== stored) return;
    fellBack = true;
    prefs.asset = DEFAULT_PREFS.asset;
    persist();
    client.open(prefs.asset);
  });
}

const client: StreamClient = new StreamClient({
  url: streamUrl,
  onMessage: (m, ignoredOverlays) => {
    if (m.type === "snapshot") snapshotSeen = true;
    controller.handle(m);
    for (let i = 0; i < ignoredOverlays; i++) controller.noteDecodeIssue("ignored");
  },
  onDecodeIssue: (r) => controller.noteDecodeIssue(r),
  onState: (s) => {
    conn = s;
    surface.setDimmed(s === "stale" || s === "reconnecting");
    if (s === "reconnecting") maybeFallBack();
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
  set("data-pane-count", String(surface.paneCount()));
}

// Each distinct error is logged once, so a persistent failure cannot spam the console per frame.
const report = createErrorReporter((e) => console.error("stonx_chart: frame step failed", e));

function frame(): void {
  // Reschedule first: nothing below may stop the loop.
  requestAnimationFrame(frame);
  let flushed = false;
  runFrame(
    [
      () => {
        controller.flush();
        flushed = true;
      },
      // The data attributes describe a flush that was applied; skip them when it failed.
      () => {
        if (!flushed) return;
        const s = controller.state();
        renderChartAttrs(s.asset, s.interval, s.barCount);
      },
      () => controls.render(controller.state(), conn),
      () => legend.render(controller.state(), conn),
      () => readout.render(controller.state(), conn),
      () => statusBar.render(controller.state(), conn),
      () => banners.render(controller.state(), conn),
    ],
    report,
  );
}
requestAnimationFrame(frame);

surface.onCrosshair((t) => runFrame([() => readout.setCrosshair(t)], report));
surface.onViewChange(() => runFrame([() => controls.render(controller.state(), conn)], report));
window.setInterval(() => runFrame([() => statusBar.render(controller.state(), conn)], report), 1000);
window.addEventListener("pagehide", () => client.close());
window.addEventListener("pageshow", (e) => {
  if (e.persisted) client.open(prefs.asset); // restored from the back/forward cache
});

client.open(prefs.asset);
