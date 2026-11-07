import { describe, expect, test } from "vitest";
import { BarStore, densify, isBar, type Bar } from "../src/bars/barStore";
import { INTERVALS, bucketOf } from "../src/time";
import { mulberry32 } from "./rng";

const T0 = 1_793_975_400_000;
const bar = (t: number, over: Partial<Bar> = {}): Bar => ({ t, o: 1, h: 2, l: 0.5, c: 1.5, vol: 10, ...over });

describe("time + BarStore", () => {
  test("bucketOf", () => {
    expect(bucketOf(T0 + 37_250, 60)).toBe(T0);
    expect(bucketOf(T0 + 37_250, 5)).toBe(T0 + 35_000);
    expect(bucketOf(T0 + 37_250, 1)).toBe(T0 + 37_000);
  });

  test("applyBar outcomes", () => {
    const s = new BarStore();
    expect(s.applyBar(bar(T0))).toBe("appended");
    expect(s.applyBar(bar(T0, { c: 2 }))).toBe("replaced");
    expect(s.applyBar(bar(T0 - 1000))).toBe("stale");
    expect(s.size).toBe(1);
    expect(s.aggregate(1)).toEqual([bar(T0, { c: 2 })]);
  });

  test("aggregates 1s into 5s", () => {
    const s = new BarStore();
    for (let i = 0; i < 5; i++) s.applyBar({ t: T0 + i * 1000, o: i + 1, h: 10 + i, l: 0 - i, c: i, vol: 1 });
    expect(s.aggregate(5)).toEqual([{ t: T0, o: 1, h: 14, l: -4, c: 4, vol: 5 }]);
  });

  test("incremental equals full aggregation (2,000 seeded ops)", () => {
    const rng = mulberry32(42);
    const s = new BarStore();
    let t = T0;
    for (let n = 0; n < 2000; n++) {
      const replace = s.size > 0 && rng() < 0.3;
      if (!replace) t += (1 + Math.floor(rng() * 3)) * 1000;
      const base = rng() * 100;
      const b: Bar = { t, o: base, h: base + rng() * 5, l: base - rng() * 5, c: base + rng() - 0.5, vol: Math.floor(rng() * 1000) };
      expect(s.applyBar(b)).toBe(replace ? "replaced" : "appended");
      for (const I of INTERVALS) {
        expect(s.bucketBar(I, bucketOf(s.lastMs()!, I))).toEqual(s.aggregate(I).at(-1));
      }
    }
  });

  test("bucketBar is undefined for an empty bucket", () => {
    const s = new BarStore();
    expect(s.bucketBar(5, T0)).toBeUndefined();
    s.applyBar(bar(T0));
    expect(s.bucketBar(5, T0 + 5000)).toBeUndefined();
    expect(s.bucketBar(5, T0 - 5000)).toBeUndefined();
  });

  test("barBuckets lists distinct buckets with data", () => {
    const s = new BarStore();
    for (const dt of [0, 1000, 7000, 8000, 130_000]) s.applyBar(bar(T0 + dt));
    expect(s.barBuckets(5)).toEqual([T0, T0 + 5000, T0 + 130_000]);
    expect(s.barBuckets(60)).toEqual([T0, T0 + 120_000]);
  });

  test("returned arrays and bars are fresh copies", () => {
    const s = new BarStore();
    s.applyBar(bar(T0));
    s.aggregate(1)[0]!.c = 999;
    s.aggregate(1).pop();
    s.barBuckets(1).push(1);
    s.bucketBar(1, T0)!.c = 999;
    expect(s.aggregate(1)).toEqual([bar(T0)]);
    expect(s.size).toBe(1);
  });

  test("densify fills whitespace inclusively", () =>
    expect(densify([bar(T0), bar(T0 + 3000)], 1, T0, T0 + 3000).map((x) => (isBar(x) ? "b" : "w"))).toEqual(["b", "w", "w", "b"]));

  test("densify empty range", () => expect(densify([], 1, T0 + 1000, T0)).toEqual([]));

  test("densify uses bucket of from/to and whitespace carries bucket time", () => {
    const out = densify([bar(T0 + 60_000)], 60, T0 + 5000, T0 + 130_000);
    expect(out.map((x) => x.t)).toEqual([T0, T0 + 60_000, T0 + 120_000]);
    expect(out.map(isBar)).toEqual([false, true, false]);
  });

  test("trimBefore drops older bars", () => {
    const s = new BarStore();
    for (let i = 0; i < 10; i++) s.applyBar(bar(T0 + i * 1000));
    s.trimBefore(T0 + 5000);
    expect(s.firstMs()).toBe(T0 + 5000);
    expect(s.size).toBe(5);
    expect(s.lastMs()).toBe(T0 + 9000);
    expect(s.applyBar(bar(T0 + 10_000))).toBe("appended");
    expect(s.size).toBe(6);
  });

  test("reset replaces state even with older times", () => {
    const s = new BarStore();
    s.applyBar(bar(T0 + 60_000));
    s.reset({ t: [T0], o: [1], h: [2], l: [0], c: [1], vol: [3] });
    expect(s.lastMs()).toBe(T0);
    expect(s.size).toBe(1);
  });

  test("grows past initial capacity and handles empty store", () => {
    const s = new BarStore();
    expect(s.firstMs()).toBeNull();
    expect(s.lastMs()).toBeNull();
    for (let i = 0; i < 5000; i++) s.applyBar(bar(T0 + i * 1000, { vol: i }));
    expect(s.size).toBe(5000);
    expect(s.aggregate(300).reduce((a, b) => a + b.vol, 0)).toBe((4999 * 5000) / 2);
  });
});
