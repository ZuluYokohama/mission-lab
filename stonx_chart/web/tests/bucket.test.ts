import { describe, expect, test } from "vitest";
import { aggregatePoints, bandRender, bucketValue, placeMarkers } from "../src/overlay/bucket";
import type { Agg, BandSpec, MarkerSpec, PointCols } from "../src/protocol/types";

const T0 = 1_793_975_400_000;

const band = (start_ms: number, end_ms: number | null): BandSpec => ({
  id: "band.x",
  kind: "band",
  group: "regime",
  pane: "price",
  start_ms,
  end_ms,
  color: "#f00",
  label: "x",
});

const mk = (id: string, time_ms: number): MarkerSpec => ({
  id,
  kind: "marker",
  group: "events",
  pane: "price",
  time_ms,
  position: "above",
  shape: "circle",
  color: "#0f0",
  text: id,
});

describe("overlay bucketing", () => {
  test("aggregatePoints per agg", () => {
    const c: PointCols = { t: [T0, T0 + 1000, T0 + 5000], v: [3, 1, 7] };
    expect(aggregatePoints(c, 5, "last")).toEqual([
      { t: T0, v: 1 },
      { t: T0 + 5000, v: 7 },
    ]);
    expect(aggregatePoints(c, 5, "mean")[0]).toEqual({ t: T0, v: 2 });
    expect(aggregatePoints(c, 5, "min")[0]).toEqual({ t: T0, v: 1 });
    expect(aggregatePoints(c, 5, "max")[0]).toEqual({ t: T0, v: 3 });
    expect(aggregatePoints(c, 5, "sum")[0]).toEqual({ t: T0, v: 4 });
    expect(aggregatePoints({ t: [], v: [] }, 5, "last")).toEqual([]);
  });

  test("bucketValue matches aggregate for live bucket", () => {
    const c: PointCols = { t: [T0, T0 + 1000, T0 + 5000, T0 + 6000, T0 + 8000], v: [3, 1, 7, 2.5, 4.25] };
    for (const agg of ["last", "mean", "min", "max", "sum"] as Agg[]) {
      expect(bucketValue(c, 5, agg, T0 + 5000)).toBe(aggregatePoints(c, 5, agg).at(-1)!.v);
    }
    expect(bucketValue(c, 5, "sum", T0)).toBe(4);
    expect(bucketValue(c, 5, "sum", T0 + 10_000)).toBeUndefined();
    expect(bucketValue(c, 5, "sum", T0 - 5000)).toBeUndefined();
    expect(bucketValue({ t: [], v: [] }, 5, "last", T0)).toBeUndefined();
  });

  test("bandRender snaps to buckets", () => {
    expect(bandRender(band(T0 + 10_500, T0 + 12_000), 1)).toMatchObject({ startMs: T0 + 10_000, endMs: T0 + 11_000 });
    expect(bandRender(band(T0 + 10_500, T0 + 12_000), 5)).toMatchObject({ startMs: T0 + 10_000, endMs: T0 + 10_000 });
    expect(bandRender(band(T0, null), 60).endMs).toBeNull();
    expect(bandRender(band(T0, null), 60)).toMatchObject({ id: "band.x", pane: "price", color: "#f00", label: "x" });
  });

  test("placeMarkers snapping", () => {
    const r = placeMarkers([mk("a", T0 + 1500), mk("b", T0 + 2500), mk("c", T0 + 9000)], [T0 + 1000, T0 + 4000], 1);
    expect(r.placed.map((m) => [m.id, m.timeMs])).toEqual([
      ["a", T0 + 1000],
      ["b", T0 + 4000],
    ]);
    expect(r.pending).toBe(1);
    expect(r.placed[0]).toMatchObject({ position: "above", shape: "circle", color: "#0f0", text: "a" });
  });

  test("same-bucket markers keep arrival order", () => {
    const r = placeMarkers([mk("z", T0 + 2200), mk("a", T0 + 100), mk("m", T0 + 2900)], [T0, T0 + 2000], 1);
    expect(r.placed.map((m) => m.id)).toEqual(["a", "z", "m"]);
    expect(r.pending).toBe(0);
  });
});
