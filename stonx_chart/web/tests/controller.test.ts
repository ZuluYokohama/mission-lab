import { describe, expect, test } from "vitest";
import { ChartController, WINDOW_MS } from "../src/app/controller";
import { isBar, type Bar, type CandleItem } from "../src/bars/barStore";
import type {
  BandSpec,
  BarCols,
  LevelSpec,
  MarkerSpec,
  OverlaySpec,
  PaneSpec,
  PointCols,
  SeriesSpec,
  StreamMessage,
} from "../src/protocol/types";
import { FakeSurface } from "../src/surface/fakeSurface";
import type { IntervalSec } from "../src/time";

/** 300 s-aligned session start. */
const T0 = 1_793_975_400_000;

const env = (seq: number) => ({ v: 1 as const, source: "SIMULATED_DEMO", seq });

const bar = (t: number, over: Partial<Bar> = {}): Bar => ({ t, o: 10, h: 12, l: 9, c: 11, vol: 100, ...over });

function cols(bars: Bar[]): BarCols {
  return {
    t: bars.map((b) => b.t),
    o: bars.map((b) => b.o),
    h: bars.map((b) => b.h),
    l: bars.map((b) => b.l),
    c: bars.map((b) => b.c),
    vol: bars.map((b) => b.vol),
  };
}

const pane = (id: string, group: string, order = 1): PaneSpec => ({
  id, kind: "pane", group, pane: "price", title: id, height_px: 120, order,
});
const series = (id: string, group: string, p = "price", over: Partial<SeriesSpec> = {}): SeriesSpec => ({
  id, kind: "series", group, pane: p, style: "line", color: "#fff", width: 1, agg: "last", precision: 2, ...over,
});
const level = (id: string, group: string, p: string): LevelSpec => ({
  id, kind: "level", group, pane: p, value: 0.5, color: "#888", label: id, dash: true,
});
const band = (id: string, group: string, start_ms: number, end_ms: number | null, p = "price"): BandSpec => ({
  id, kind: "band", group, pane: p, start_ms, end_ms, color: "state.halt", label: id,
});
const marker = (id: string, time_ms: number, group = "events"): MarkerSpec => ({
  id, kind: "marker", group, pane: "price", time_ms, position: "above", shape: "arrowDown", color: "#f00", text: id,
});

/** The standard overlay set: a lambda2 pane with its series and level, bid/ask and a vwap on price. */
const STD: OverlaySpec[] = [
  pane("pane.lambda2", "lambda2"),
  series("series.lambda2", "lambda2", "pane.lambda2"),
  series("series.bid", "quotes"),
  series("series.ask", "quotes"),
  level("level.lambda2.coherent", "lambda2", "pane.lambda2"),
];

function snapshot(
  seq: number,
  o: { bars?: Bar[]; overlays?: OverlaySpec[]; points?: Record<string, PointCols>; asset?: string } = {},
): StreamMessage {
  return {
    ...env(seq),
    type: "snapshot",
    asset: o.asset ?? "NVDA",
    assets: [{ id: 1, symbol: "NVDA" }, { id: 2, symbol: "AMD" }],
    session_start_ms: T0,
    bars: cols(o.bars ?? []),
    overlays: o.overlays ?? STD,
    points: o.points ?? {},
  };
}
const barMsg = (seq: number, b: Bar): StreamMessage => ({ ...env(seq), type: "bar", ...b });
const ptsMsg = (seq: number, id: string, t: number[], v: number[]): StreamMessage => ({
  ...env(seq), type: "series.points", id, points: { t, v },
});
const upsertMsg = (seq: number, overlay: OverlaySpec): StreamMessage => ({ ...env(seq), type: "overlay.upsert", overlay });
const removeMsg = (seq: number, id: string): StreamMessage => ({ ...env(seq), type: "overlay.remove", id });

const range = (from: number, n: number, step = 1000): number[] => Array.from({ length: n }, (_, i) => from + i * step);
const barsAt = (ts: number[]): Bar[] => ts.map((t, i) => bar(t, { c: 11 + i }));

const methods = (f: FakeSurface): string[] => f.calls.map((c) => c.method);
const callsOf = (f: FakeSurface, m: string): unknown[][] => f.calls.filter((c) => c.method === m).map((c) => c.args);
const lastCall = (f: FakeSurface, m: string): unknown[] | undefined => callsOf(f, m).at(-1);
const seriesIdsSet = (f: FakeSurface): string[] => callsOf(f, "setSeries").map((a) => (a[0] as SeriesSpec).id).sort();

function setup(o: { interval?: IntervalSec; hiddenGroups?: string[]; windowMs?: number; now?: () => number } = {}) {
  const fake = new FakeSurface();
  const c = new ChartController(fake, { interval: 1, ...o });
  return { fake, c };
}

/** Handle `msg`, flush, then forget the calls so the next flush is observed alone. */
function primed(c: ChartController, fake: FakeSurface, ...msgs: StreamMessage[]): void {
  for (const m of msgs) c.handle(m);
  c.flush();
  fake.calls.length = 0;
}

describe("ChartController", () => {
  test("snapshot issues one full render", () => {
    const { fake, c } = setup();
    c.handle(snapshot(1, { bars: barsAt(range(T0, 5)), points: { "series.lambda2": { t: range(T0, 5), v: [1, 2, 3, 4, 5] } } }));
    expect(fake.calls).toEqual([]);
    expect(c.flush()).toEqual({ fullReset: true });
    expect(methods(fake)).toEqual([
      "isAtLiveEdge", "getVisibleRange", "setCandles", "setPanes",
      "setSeries", "setSeries", "setSeries",
      "setLevels", "setBands", "setMarkers", "scrollToLive",
    ]);
    expect(seriesIdsSet(fake)).toEqual(["series.ask", "series.bid", "series.lambda2"]);
    expect(fake.candles.map((x) => x.t)).toEqual(range(T0, 5));
    expect(fake.panes.map((p) => p.id)).toEqual(["pane.lambda2"]);
    expect(fake.series.get("series.lambda2")!.data).toEqual(range(T0, 5).map((t, i) => ({ t, v: i + 1 })));
    expect(fake.levels.map((l) => l.id)).toEqual(["level.lambda2.coherent"]);
    expect(c.state()).toMatchObject({ asset: "NVDA", assets: ["NVDA", "AMD"], source: "SIMULATED_DEMO", interval: 1, barCount: 5 });
  });

  test("default interval is 60", () => {
    expect(new ChartController(new FakeSurface()).state().interval).toBe(60);
    expect(WINDOW_MS).toBe(23_400_000);
  });

  test("repeated bar updates collapse into one updateCandle", () => {
    const { fake, c } = setup();
    primed(c, fake, snapshot(1, { bars: [bar(T0)] }));
    for (let i = 0; i < 50; i++) c.handle(barMsg(2 + i, bar(T0 + 1000, { c: 100 + i })));
    expect(c.flush()).toEqual({ fullReset: false });
    expect(methods(fake)).toEqual(["updateCandle"]);
    expect(fake.candles.at(-1)).toEqual(bar(T0 + 1000, { c: 149 }));
    expect(fake.candles).toHaveLength(2);
  });

  test("new bucket after gap appends whitespace then bar", () => {
    const { fake, c } = setup();
    primed(c, fake, snapshot(1, { bars: [bar(T0)] }));
    c.handle(barMsg(2, bar(T0 + 3000)));
    c.flush();
    expect(callsOf(fake, "updateCandle")).toEqual([[{ t: T0 + 1000 }], [{ t: T0 + 2000 }], [bar(T0 + 3000)]]);
    expect(methods(fake)).toEqual(["updateCandle", "updateCandle", "updateCandle"]);
    expect(c.state().barCount).toBe(2);
  });

  test("a dirty bar in the last bucket emits one updateCandle at higher intervals", () => {
    const { fake, c } = setup({ interval: 60 });
    primed(c, fake, snapshot(1, { bars: [bar(T0)] }));
    c.handle(barMsg(2, bar(T0 + 1000, { h: 20, c: 15, vol: 5 })));
    c.handle(barMsg(3, bar(T0 + 2000, { l: 1, c: 13, vol: 7 })));
    c.flush();
    expect(callsOf(fake, "updateCandle")).toEqual([[{ t: T0, o: 10, h: 20, l: 1, c: 13, vol: 112 }]]);
  });

  describe.each(["snapshot", "stream"] as const)("halt gap renders whitespace and band (%s)", (mode) => {
    test("candles hold whitespace across the halt and the band covers it", () => {
      const { fake, c } = setup();
      const haltPts = { t: range(T0 + 5000, 5), v: [1, 2, 3, 4, 5] };
      const halt = band("band.halt.1", "halt", T0 + 5000, T0 + 9500);
      if (mode === "snapshot") {
        c.handle(snapshot(1, { bars: barsAt(range(T0, 5)), overlays: [...STD, halt], points: { "series.lambda2": haltPts } }));
      } else {
        primed(c, fake, snapshot(1, { bars: barsAt(range(T0, 5)) }));
        c.handle(upsertMsg(2, band("band.halt.1", "halt", T0 + 5000, null)));
        for (let i = 0; i < 5; i++) c.handle(ptsMsg(3 + i, "series.lambda2", [haltPts.t[i]!], [haltPts.v[i]!]));
        c.handle(upsertMsg(8, halt));
      }
      expect(c.flush().fullReset).toBe(mode === "snapshot");
      expect(fake.candles.map((x) => x.t)).toEqual(range(T0, 10));
      expect(fake.candles.slice(0, 5).every(isBar)).toBe(true);
      expect(fake.candles.slice(5)).toEqual(range(T0 + 5000, 5).map((t) => ({ t })));
      expect(fake.bands).toHaveLength(1);
      expect(fake.bands[0]).toMatchObject({ id: "band.halt.1", startMs: T0 + 5000, endMs: T0 + 9000 });
      expect(fake.series.get("series.lambda2")!.data.map((p) => p.t)).toEqual(haltPts.t);
      expect(c.state().barCount).toBe(5);
    });
  });

  test("interval switch re-aggregates and keeps range", () => {
    const { fake, c } = setup();
    primed(c, fake, snapshot(1, { bars: barsAt(range(T0, 60)), points: { "series.bid": { t: range(T0, 60), v: range(0, 60, 1) } } }));
    fake.atLiveEdge = false;
    fake.visibleRange = { fromMs: T0, toMs: T0 + 60_000 };
    c.setInterval(5);
    expect(c.flush()).toEqual({ fullReset: true });
    expect(lastCall(fake, "setVisibleRange")).toEqual([{ fromMs: T0, toMs: T0 + 60_000 }]);
    expect(methods(fake)).not.toContain("scrollToLive");
    expect(fake.candles.map((x) => x.t)).toEqual(range(T0, 12, 5000));
    expect(fake.candles[0]).toEqual({ t: T0, o: 10, h: 12, l: 9, c: 15, vol: 500 });
    expect(fake.series.get("series.bid")!.data).toEqual(range(T0, 12, 5000).map((t, i) => ({ t, v: i * 5 + 4 })));
    expect(c.state()).toMatchObject({ interval: 5, barCount: 12 });
    c.setInterval(5);
    fake.calls.length = 0;
    expect(c.flush()).toEqual({ fullReset: false });
    expect(fake.calls).toEqual([]);
  });

  test("hidden group never reaches the surface", () => {
    const { fake, c } = setup({ hiddenGroups: ["quotes"] });
    c.handle(snapshot(1, { bars: [bar(T0)], points: { "series.bid": { t: [T0], v: [1] } } }));
    c.flush();
    expect(seriesIdsSet(fake)).toEqual(["series.lambda2"]);
    fake.calls.length = 0;
    c.handle(ptsMsg(2, "series.bid", [T0 + 1000], [2]));
    c.handle(ptsMsg(3, "series.ask", [T0 + 1000], [3]));
    c.flush();
    expect(methods(fake)).toEqual(["updateCandle"]);
    expect(c.state().hiddenGroups).toEqual(["quotes"]);

    fake.calls.length = 0;
    c.setGroupVisible("quotes", true);
    expect(c.flush()).toEqual({ fullReset: true });
    expect(seriesIdsSet(fake)).toEqual(["series.ask", "series.bid", "series.lambda2"]);
    expect(fake.series.get("series.bid")!.data).toEqual([{ t: T0, v: 1 }, { t: T0 + 1000, v: 2 }]);
    expect(c.state().hiddenGroups).toEqual([]);

    fake.calls.length = 0;
    c.setGroupVisible("quotes", false);
    c.flush();
    expect(callsOf(fake, "removeSeries").map((a) => a[0]).sort()).toEqual(["series.ask", "series.bid"]);
    expect([...fake.series.keys()]).toEqual(["series.lambda2"]);
  });

  test("hiding a pane's group omits the pane, its series and its levels", () => {
    const { fake, c } = setup();
    primed(c, fake, snapshot(1, { bars: [bar(T0)] }));
    c.setGroupVisible("lambda2", false);
    c.flush();
    expect(fake.panes).toEqual([]);
    expect(fake.levels).toEqual([]);
    expect([...fake.series.keys()].sort()).toEqual(["series.ask", "series.bid"]);
  });

  test("a pane hosting no visible series is omitted, with its levels", () => {
    const { fake, c } = setup();
    // The pane's own group is visible, but its only series belongs to a hidden group.
    const overlays = [pane("pane.x", "x"), series("series.x", "xs", "pane.x"), level("level.x", "x", "pane.x")];
    c.setGroupVisible("xs", false);
    c.handle(snapshot(1, { bars: [bar(T0)], overlays }));
    c.flush();
    expect(fake.panes).toEqual([]);
    expect(fake.levels).toEqual([]);
    expect(fake.series.size).toBe(0);
  });

  test("pending marker appears when its bar arrives", () => {
    const { fake, c } = setup();
    c.handle(snapshot(1, { bars: barsAt(range(T0, 3)), overlays: [...STD, marker("marker.reset.1", T0 + 5000)] }));
    c.flush();
    expect(lastCall(fake, "setMarkers")).toEqual([[]]);
    fake.calls.length = 0;
    c.handle(barMsg(2, bar(T0 + 3000)));
    c.flush();
    expect(methods(fake)).not.toContain("setMarkers");
    c.handle(barMsg(3, bar(T0 + 5000)));
    c.flush();
    expect(fake.markers).toEqual([
      { id: "marker.reset.1", timeMs: T0 + 5000, position: "above", shape: "arrowDown", color: "#f00", text: "marker.reset.1" },
    ]);
    fake.calls.length = 0;
    c.handle(barMsg(4, bar(T0 + 6000)));
    c.flush();
    expect(methods(fake)).toEqual(["updateCandle"]);
  });

  test("marker upsert and remove re-issue setMarkers; markers snap to real bars only", () => {
    const { fake, c } = setup();
    // Points extend the grid to T0+4000 with whitespace; the marker must still snap to a real bar.
    primed(c, fake, snapshot(1, { bars: [bar(T0), bar(T0 + 4000)], points: { "series.bid": { t: range(T0, 5), v: [1, 2, 3, 4, 5] } } }));
    c.handle(upsertMsg(2, marker("m", T0 + 1000)));
    c.flush();
    expect(methods(fake)).toEqual(["setMarkers"]);
    expect(fake.markers.map((m) => m.timeMs)).toEqual([T0 + 4000]);
    c.handle(removeMsg(3, "m"));
    c.flush();
    expect(fake.markers).toEqual([]);
  });

  test("counters", () => {
    let now = 1000;
    const { fake, c } = setup({ now: () => now });
    c.connectionOpened();
    c.handle(snapshot(7, { bars: [bar(T0)] }));
    expect(c.state().lastMessageAtMs).toBe(1000);
    c.handle(barMsg(8, bar(T0 + 1000)));
    now = 2000;
    c.handle(barMsg(10, bar(T0 + 2000))); // seq jump
    c.handle(barMsg(11, bar(T0 + 1000))); // older bar
    c.handle(ptsMsg(12, "series.unknown", [T0], [1]));
    c.handle(ptsMsg(13, "series.bid", [T0 + 2000], [1]));
    c.handle(ptsMsg(14, "series.bid", [T0 + 1000], [1])); // older than the series' last point
    c.handle(upsertMsg(15, { ...marker("m", T0), pane: "pane.lambda2" })); // markers live on price only
    expect(c.counters).toEqual({ malformed: 0, ignored: 3, gaps: 1, stale: 1 });
    expect(c.state().lastMessageAtMs).toBe(2000);
    c.noteDecodeIssue("malformed");
    c.noteDecodeIssue("ignored");
    expect(c.counters).toEqual({ malformed: 1, ignored: 4, gaps: 1, stale: 1 });
    expect(c.state().counters).toEqual({ malformed: 1, ignored: 4, gaps: 1, stale: 1 });
    expect(c.state().counters).not.toBe(c.counters); // a copy, not the live object
    c.flush();
    expect(fake.candles.map((x) => x.t)).toEqual(range(T0, 3));

    c.connectionOpened();
    c.handle(snapshot(1, { bars: [bar(T0)], overlays: [...STD, series("series.held", "g", "pane.missing")] }));
    expect(c.counters).toMatchObject({ gaps: 1, ignored: 5 }); // new connection re-arms seq; held spec dropped
    c.handle(barMsg(2, bar(T0 + 1000)));
    expect(c.counters.gaps).toBe(1);
  });

  test("status and heartbeat update state", () => {
    let now = 5;
    const { c } = setup({ now: () => now });
    c.handle({ ...env(1), type: "status", feed_state: "degraded", drop_count: 3, message: "source error" });
    expect(c.state()).toMatchObject({ feedState: "degraded", dropCount: 3, statusMessage: "source error", lastMessageAtMs: 5 });
    now = 9;
    c.handle({ ...env(2), type: "heartbeat", now_ms: 9 });
    expect(c.state().lastMessageAtMs).toBe(9);
    c.handle({ ...env(3), type: "status", feed_state: "ok", drop_count: 3 });
    expect(c.state()).toMatchObject({ feedState: "ok", dropCount: 3, statusMessage: null });
    // `source` rides on every envelope; asset and assets come only from a snapshot.
    expect(c.state()).toMatchObject({ asset: null, assets: [], source: "SIMULATED_DEMO", barCount: 0 });
  });

  test("burst of messages collapses into bounded surface calls", () => {
    const { fake, c } = setup();
    primed(c, fake, snapshot(1, { bars: [bar(T0)], points: { "series.lambda2": { t: [T0], v: [0] }, "series.bid": { t: [T0], v: [0] } } }));
    let seq = 2;
    for (let i = 0; i < 10_000; i++) {
      const t = T0 + 1000 + Math.floor(i / 1000) * 1000;
      if (i % 3 === 0) c.handle(barMsg(seq++, bar(t, { c: i })));
      else c.handle(ptsMsg(seq++, i % 3 === 1 ? "series.lambda2" : "series.bid", [t], [i]));
    }
    c.flush();
    expect(callsOf(fake, "updateCandle").length).toBeLessThanOrEqual(10);
    for (const id of ["series.lambda2", "series.bid"]) {
      expect(callsOf(fake, "updateSeriesPoint").filter((a) => a[0] === id).length).toBeLessThanOrEqual(10);
    }
    expect(fake.candles).toHaveLength(11);
    expect(fake.series.get("series.bid")!.data).toHaveLength(11);
    expect(methods(fake).every((m) => m === "updateCandle" || m === "updateSeriesPoint")).toBe(true);
  });

  test("trims past 110% of window and keeps live edge", () => {
    // R4: 13 one-second bars at B..B+12000, windowMs 10_000. The cut is 300 s-aligned, so B sits 2 s
    // before a 300 s boundary for the cut (bucketOf(B+12000-10000, 300) = B+2000) to drop anything.
    const B = T0 + 298_000;
    const { fake, c } = setup({ windowMs: 10_000 });
    primed(c, fake, snapshot(1, { bars: [bar(B)], points: { "series.bid": { t: [B], v: [1] } } }));
    let sawFull = false;
    for (let i = 1; i <= 12; i++) {
      c.handle(barMsg(1 + i, bar(B + i * 1000)));
      c.handle(ptsMsg(100 + i, "series.bid", [B + i * 1000], [i]));
      sawFull = c.flush().fullReset || sawFull;
    }
    expect(sawFull).toBe(true);
    expect(c.state().barCount).toBeLessThanOrEqual(11);
    expect(c.state().barCount).toBe(11);
    const m = methods(fake);
    expect(m).toContain("setCandles");
    expect(m.lastIndexOf("scrollToLive")).toBeGreaterThan(m.lastIndexOf("setCandles"));
    expect(fake.candles[0]!.t).toBe(B + 2000);
    expect(fake.series.get("series.bid")!.data[0]!.t).toBe(B + 2000);
  });

  test("a 300 s-aligned cut that would drop nothing does not trim or reset", () => {
    const { fake, c } = setup({ windowMs: 10_000 });
    primed(c, fake, snapshot(1, { bars: [bar(T0)] }));
    for (let i = 1; i <= 12; i++) {
      c.handle(barMsg(1 + i, bar(T0 + i * 1000)));
      expect(c.flush().fullReset).toBe(false);
    }
    expect(methods(fake)).not.toContain("setCandles");
    expect(c.state().barCount).toBe(13);
  });

  test("later snapshot with older times replaces state", () => {
    const { fake, c } = setup();
    c.handle(snapshot(1, { bars: [bar(T0 + 600_000)], points: { "series.bid": { t: [T0 + 600_000], v: [1] } } }));
    c.flush();
    c.handle(snapshot(1, { bars: [bar(T0)], points: { "series.bid": { t: [T0], v: [2] } } }));
    expect(c.flush()).toEqual({ fullReset: true });
    expect(fake.candles[0]!.t).toBe(T0);
    expect(fake.candles).toHaveLength(1);
    expect(fake.series.get("series.bid")!.data).toEqual([{ t: T0, v: 2 }]);
    expect(c.counters.stale).toBe(0);
  });

  test("a later snapshot removes series it no longer has", () => {
    const { fake, c } = setup();
    primed(c, fake, snapshot(1, { bars: [bar(T0)] }));
    c.handle(snapshot(1, { bars: [bar(T0)], overlays: [series("series.bid", "quotes")] }));
    c.flush();
    expect(callsOf(fake, "removeSeries").map((a) => a[0]).sort()).toEqual(["series.ask", "series.lambda2"]);
    expect([...fake.series.keys()]).toEqual(["series.bid"]);
    expect(fake.panes).toEqual([]);
    expect(fake.levels).toEqual([]);
  });

  test("readoutAt", () => {
    const { fake, c } = setup({ interval: 5, hiddenGroups: ["quotes"] });
    c.handle(
      snapshot(1, {
        bars: [bar(T0, { h: 15 }), bar(T0 + 1000, { l: 2, c: 13 }), bar(T0 + 5000)],
        points: { "series.lambda2": { t: [T0, T0 + 1000], v: [0.25, 0.5] }, "series.bid": { t: [T0], v: [99] } },
      }),
    );
    c.flush();
    expect(fake.series.has("series.bid")).toBe(false);
    expect(c.readoutAt(T0 + 3500)).toEqual({
      bar: { t: T0, o: 10, h: 15, l: 2, c: 13, vol: 200 },
      values: [{ id: "series.lambda2", group: "lambda2", value: 0.5 }],
    });
    expect(c.readoutAt(T0 + 5000)).toEqual({ bar: bar(T0 + 5000), values: [] });
    expect(c.readoutAt(T0 + 60_000)).toBeNull();
  });

  test("flush with nothing dirty makes no calls", () => {
    const { fake, c } = setup();
    c.flush();
    fake.calls.length = 0;
    c.flush();
    expect(fake.calls).toEqual([]);
    c.handle(snapshot(1, { bars: [bar(T0)] }));
    c.flush();
    fake.calls.length = 0;
    expect(c.flush()).toEqual({ fullReset: false });
    expect(fake.calls).toEqual([]);
  });

  describe("upsert-replace of an existing overlay re-renders it", () => {
    test("a snapshot band re-upserted with an end_ms", () => {
      const { fake, c } = setup();
      const open = band("band.halt.1", "halt", T0 + 1000, null);
      primed(c, fake, snapshot(1, { bars: barsAt(range(T0, 5)), overlays: [...STD, open] }));
      c.handle(upsertMsg(2, { ...open, end_ms: T0 + 3000 }));
      c.flush();
      expect(methods(fake)).toEqual(["setBands"]);
      expect(fake.bands).toEqual([{ id: "band.halt.1", pane: "price", color: "state.halt", label: "band.halt.1", startMs: T0 + 1000, endMs: T0 + 2000 }]);
    });

    test("a series spec replaced with a new color", () => {
      const { fake, c } = setup();
      primed(c, fake, snapshot(1, { bars: [bar(T0)], points: { "series.bid": { t: [T0], v: [1] } } }));
      c.handle(ptsMsg(2, "series.bid", [T0 + 1000], [2]));
      c.handle(upsertMsg(3, series("series.bid", "quotes", "price", { color: "#123456" })));
      c.flush();
      expect(callsOf(fake, "setSeries")).toEqual([
        [series("series.bid", "quotes", "price", { color: "#123456" }), [{ t: T0, v: 1 }, { t: T0 + 1000, v: 2 }]],
      ]);
      expect(callsOf(fake, "updateSeriesPoint").filter((a) => a[0] === "series.bid")).toEqual([]);
      expect(methods(fake)).not.toContain("removeSeries");
      expect(fake.series.get("series.bid")!.spec.color).toBe("#123456");
      expect([...fake.series.keys()]).toEqual(["series.lambda2", "series.bid", "series.ask"]);
    });

    test("a level replaced with a new value", () => {
      const { fake, c } = setup();
      primed(c, fake, snapshot(1, { bars: [bar(T0)] }));
      c.handle(upsertMsg(2, { ...level("level.lambda2.coherent", "lambda2", "pane.lambda2"), value: 0.9 }));
      c.flush();
      expect(methods(fake)).toEqual(["setLevels"]);
      expect(fake.levels.map((l) => l.value)).toEqual([0.9]);
    });
  });

  test("structural changes: new pane releases a held series; removal drops series and empty pane", () => {
    const { fake, c } = setup();
    primed(c, fake, snapshot(1, { bars: [bar(T0)] }));
    c.handle(upsertMsg(2, series("series.curl", "curl", "pane.curl")));
    c.flush();
    expect(fake.calls).toEqual([]); // held: its pane is missing
    c.handle(upsertMsg(3, pane("pane.curl", "curl", 2)));
    c.handle(ptsMsg(4, "series.curl", [T0], [0.7]));
    c.flush();
    expect(methods(fake)).toEqual(["setPanes", "setSeries", "setLevels"]);
    expect(fake.panes.map((p) => p.id)).toEqual(["pane.lambda2", "pane.curl"]);
    expect(fake.series.get("series.curl")!.data).toEqual([{ t: T0, v: 0.7 }]);

    fake.calls.length = 0;
    c.handle(removeMsg(5, "series.curl"));
    c.flush();
    expect(methods(fake)).toEqual(["removeSeries", "setPanes", "setLevels"]);
    expect(fake.panes.map((p) => p.id)).toEqual(["pane.lambda2"]);
  });

  test("a bar older than the grid's last bucket triggers a full reset", () => {
    const { fake, c } = setup();
    // Points push the grid to T0+5000 while the last bar is at T0.
    primed(c, fake, snapshot(1, { bars: [bar(T0)], points: { "series.bid": { t: range(T0, 6), v: [1, 2, 3, 4, 5, 6] } } }));
    c.handle(barMsg(2, bar(T0 + 2000, { c: 42 })));
    expect(c.flush()).toEqual({ fullReset: true });
    const at = fake.candles.find((x: CandleItem) => x.t === T0 + 2000);
    expect(at).toEqual(bar(T0 + 2000, { c: 42 }));
    expect(fake.candles).toHaveLength(6);
  });

  test("messages before any snapshot render on the first flush", () => {
    const { fake, c } = setup();
    c.handle(barMsg(1, bar(T0)));
    expect(c.flush()).toEqual({ fullReset: true });
    expect(fake.candles).toEqual([bar(T0)]);
  });
});
