import { describe, expect, test } from "vitest";
import { OverlayStore } from "../src/overlay/overlayStore";
import { decodeMessage } from "../src/protocol/decode";
import type { BandSpec, LevelSpec, MarkerSpec, PaneSpec, SeriesSpec } from "../src/protocol/types";
import { loadFixtureMessages } from "./fixtures";

const T0 = 1_793_975_400_000;

const pane = (id: string, order = 0, group = "g"): PaneSpec => ({
  id, kind: "pane", group, pane: "price", title: id, height_px: 100, order,
});
const series = (id: string, p = "price", group = "g"): SeriesSpec => ({
  id, kind: "series", group, pane: p, style: "line", color: "#fff", width: 1, agg: "last", precision: 2,
});
const level = (id: string, p = "price", group = "g"): LevelSpec => ({
  id, kind: "level", group, pane: p, value: 1, color: "#fff", label: id, dash: false,
});
const band = (id: string, start_ms: number, end_ms: number | null, p = "price", group = "g"): BandSpec => ({
  id, kind: "band", group, pane: p, start_ms, end_ms, color: "#f00", label: id,
});
const marker = (id: string, time_ms: number, p = "price", group = "g"): MarkerSpec => ({
  id, kind: "marker", group, pane: p, time_ms, position: "above", shape: "circle", color: "#0f0", text: id,
});

describe("OverlayStore", () => {
  test("upsert replaces by id", () => {
    const s = new OverlayStore();
    expect(s.upsert(band("b", T0, null))).toBe("applied");
    expect(s.upsert(band("b", T0, T0 + 3000))).toBe("applied");
    const b = s.bands(1);
    expect(b).toHaveLength(1);
    expect(b[0]).toMatchObject({ id: "b", startMs: T0, endMs: T0 + 2000 });
  });

  test("replacing keeps first-upsert position and series points", () => {
    const s = new OverlayStore();
    s.upsert(series("a", "price", "g1"));
    s.upsert(series("b", "price", "g2"));
    s.upsert(marker("m1", T0));
    s.upsert(marker("m2", T0));
    s.appendPoints("a", [T0], [1]);
    s.upsert({ ...series("a", "price", "g3"), color: "#000" });
    s.upsert(marker("m1", T0 + 1000));
    expect(s.series().map((x) => [x.id, x.color])).toEqual([["a", "#000"], ["b", "#fff"]]);
    expect(s.markers().map((m) => [m.id, m.time_ms])).toEqual([["m1", T0 + 1000], ["m2", T0]]);
    expect(s.groups()).toEqual(["g3", "g2", "g"]);
    expect(s.points("a")).toEqual({ t: [T0], v: [1] });
  });

  test("held until pane arrives; reset drops held", () => {
    const s = new OverlayStore();
    expect(s.upsert(series("series.x", "pane.x"))).toBe("held");
    expect(s.series()).toEqual([]);
    expect(s.upsert(pane("pane.x"))).toBe("applied");
    expect(s.series().map((x) => x.id)).toEqual(["series.x"]);
    s.upsert(series("series.y", "pane.y"));
    expect(s.reset([], {}).droppedHeld).toBe(1);
    expect(s.series()).toEqual([]);
  });

  test("marker off price pane ignored", () => {
    const s = new OverlayStore();
    s.upsert(pane("pane.curl"));
    expect(s.upsert(marker("m", T0, "pane.curl"))).toBe("ignored");
    expect(new OverlayStore().upsert(marker("m", T0, "pane.nope"))).toBe("ignored");
    expect(s.markers()).toEqual([]);
  });

  test("remove covers applied and held, no pane cascade", () => {
    const s = new OverlayStore();
    s.upsert(pane("p"));
    s.upsert(series("s", "p"));
    s.upsert(series("h", "gone"));
    expect(s.remove("h")).toBe(true);
    expect(s.remove("h")).toBe(false);
    expect(s.remove("p")).toBe(true);
    expect(s.series().map((x) => x.id)).toEqual(["s"]);
    expect(s.remove("s")).toBe(true);
    expect(s.points("s")).toBeUndefined();
    expect(s.remove("nope")).toBe(false);
  });

  test("appendPoints rules", () => {
    const s = new OverlayStore();
    s.upsert(series("s"));
    expect(s.points("s")).toEqual({ t: [], v: [] });
    expect(s.appendPoints("s", [T0], [1])).toEqual({ touched: [T0], rejected: 0 });
    expect(s.appendPoints("s", [T0], [2])).toEqual({ touched: [T0], rejected: 0 });
    expect(s.points("s")).toEqual({ t: [T0], v: [2] });
    expect(s.appendPoints("s", [T0 - 1000], [9])).toEqual({ touched: [], rejected: 1 });
    expect(s.appendPoints("s", [T0 + 1000, T0 + 2000, T0 + 500], [3, 4, 5])).toEqual({
      touched: [T0 + 1000, T0 + 2000],
      rejected: 1,
    });
    expect(s.points("s")).toEqual({ t: [T0, T0 + 1000, T0 + 2000], v: [2, 3, 4] });
    expect(s.appendPoints("nope", [T0, T0 + 1000], [1, 2])).toEqual({ touched: [], rejected: 2 });
  });

  test("returned values are copies", () => {
    const s = new OverlayStore();
    s.upsert(pane("p"));
    s.upsert(series("s"));
    s.appendPoints("s", [T0], [1]);
    s.points("s")!.t.push(5);
    s.series()[0]!.color = "#abc";
    s.panes()[0]!.title = "x";
    expect(s.points("s")!.t).toEqual([T0]);
    expect(s.series()[0]!.color).toBe("#fff");
    expect(s.panes()[0]!.title).toBe("p");
  });

  test("hostile ids such as __proto__ are plain keys", () => {
    const s = new OverlayStore();
    expect(s.upsert(series("__proto__"))).toBe("applied");
    expect(s.appendPoints("__proto__", [T0], [1]).touched).toEqual([T0]);
    expect(s.points("__proto__")).toEqual({ t: [T0], v: [1] });
    expect(s.points("constructor")).toBeUndefined();
    s.reset([series("__proto__")], { ["__proto__"]: { t: [T0], v: [2] } });
    expect(s.points("__proto__")).toEqual({ t: [T0], v: [2] });
    expect(({} as Record<string, unknown>)["t"]).toBeUndefined();
  });

  test("panes sorted by order; pointsExtent", () => {
    const s = new OverlayStore();
    s.upsert(pane("b", 2));
    s.upsert(pane("a", 1));
    expect(s.panes().map((p) => p.id)).toEqual(["a", "b"]);
    expect(s.pointsExtent()).toBeNull();
    s.upsert(series("x"));
    s.upsert(series("y"));
    s.appendPoints("x", [T0 + 2000], [1]);
    s.appendPoints("y", [T0, T0 + 9000], [1, 2]);
    expect(s.pointsExtent()).toEqual({ first: T0, last: T0 + 9000 });
  });

  test("levels and reset with points", () => {
    const s = new OverlayStore();
    s.reset([pane("p"), series("s", "p"), level("l", "p")], { s: { t: [T0, T0 + 1000], v: [1, 2] }, zz: { t: [T0], v: [1] } });
    expect(s.levels().map((l) => l.id)).toEqual(["l"]);
    expect(s.points("s")).toEqual({ t: [T0, T0 + 1000], v: [1, 2] });
    expect(s.points("zz")).toBeUndefined();
  });

  test("groups in first-seen order", () => {
    const [raw] = loadFixtureMessages("nvda_snapshot_600s.json");
    const d = decodeMessage(raw!);
    if (!d.ok || d.message.type !== "snapshot") throw new Error("fixture did not decode to a snapshot");
    const s = new OverlayStore();
    const { droppedHeld } = s.reset(d.message.overlays, d.message.points);
    expect(droppedHeld).toBe(0);
    const g = s.groups();
    expect(g.slice(0, 4)).toEqual(["lambda2", "curl", "quotes", "vwap"]);
    expect(g).toEqual(expect.arrayContaining(["regime", "halt", "events"]));
    expect(s.pointsExtent()).not.toBeNull();
  });

  test("trimBefore", () => {
    const s = new OverlayStore();
    s.upsert(series("s"));
    s.appendPoints("s", [T0, T0 + 1000, T0 + 2000], [1, 2, 3]);
    s.upsert(band("closed", T0, T0 + 1500));
    s.upsert(band("edge", T0, T0 + 2000));
    s.upsert(band("open", T0, null));
    s.upsert(marker("old", T0 + 1999));
    s.upsert(marker("keep", T0 + 2000));
    s.trimBefore(T0 + 2000);
    expect(s.points("s")).toEqual({ t: [T0 + 2000], v: [3] });
    expect(s.bands(1).map((b) => b.id)).toEqual(["open"]);
    expect(s.markers().map((m) => m.id)).toEqual(["keep"]);
  });
});
