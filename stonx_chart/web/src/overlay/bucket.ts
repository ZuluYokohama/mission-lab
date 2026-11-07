/**
 * Pure interval bucketing for overlays: series points are aggregated per bucket, bands cover every
 * bucket that intersects [start_ms, end_ms), markers snap to a bucket that holds a real bar.
 */
import type { Agg, BandSpec, MarkerSpec, PointCols } from "../protocol/types";
import { bucketOf, type IntervalSec } from "../time";

export interface Point {
  t: number;
  v: number;
}

/** Fold `v[lo, hi)` (non-empty) with `agg`, left to right so every caller gets the same float result. */
function fold(v: number[], lo: number, hi: number, agg: Agg): number {
  switch (agg) {
    case "last":
      return v[hi - 1]!;
    case "min": {
      let m = v[lo]!;
      for (let i = lo + 1; i < hi; i++) if (v[i]! < m) m = v[i]!;
      return m;
    }
    case "max": {
      let m = v[lo]!;
      for (let i = lo + 1; i < hi; i++) if (v[i]! > m) m = v[i]!;
      return m;
    }
    case "sum":
    case "mean": {
      let s = 0;
      for (let i = lo; i < hi; i++) s += v[i]!;
      return agg === "mean" ? s / (hi - lo) : s;
    }
  }
}

/** Aggregate 1s points (ascending `t`) into `interval`-second buckets. */
export function aggregatePoints(cols: PointCols, interval: IntervalSec, agg: Agg): Point[] {
  const out: Point[] = [];
  const n = Math.min(cols.t.length, cols.v.length);
  let i = 0;
  while (i < n) {
    const b = bucketOf(cols.t[i]!, interval);
    let j = i + 1;
    while (j < n && bucketOf(cols.t[j]!, interval) === b) j++;
    out.push({ t: b, v: fold(cols.v, i, j, agg) });
    i = j;
  }
  return out;
}

/** Value of the single bucket starting at `bucketMs`, scanning back from the end: O(points in bucket). */
export function bucketValue(cols: PointCols, interval: IntervalSec, agg: Agg, bucketMs: number): number | undefined {
  const endMs = bucketMs + interval * 1000;
  let hi = Math.min(cols.t.length, cols.v.length);
  while (hi > 0 && cols.t[hi - 1]! >= endMs) hi--;
  let lo = hi;
  while (lo > 0 && cols.t[lo - 1]! >= bucketMs) lo--;
  return lo === hi ? undefined : fold(cols.v, lo, hi, agg);
}

export interface BandRender {
  id: string;
  pane: string;
  color: string;
  label: string;
  startMs: number;
  /** Start of the last covered bucket; null while the band is open. */
  endMs: number | null;
}

export function bandRender(spec: BandSpec, interval: IntervalSec): BandRender {
  return {
    id: spec.id,
    pane: spec.pane,
    color: spec.color,
    label: spec.label,
    startMs: bucketOf(spec.start_ms, interval),
    endMs: spec.end_ms === null ? null : bucketOf(spec.end_ms - 1, interval),
  };
}

export interface MarkerRender {
  id: string;
  /** Start of the bar bucket the marker snapped to. */
  timeMs: number;
  position: MarkerSpec["position"];
  shape: MarkerSpec["shape"];
  color: string;
  text: string;
}

/**
 * Snap each marker to the first bar bucket at or after its own bucket (`barBuckets` ascending).
 * Markers beyond the last bar are counted as pending. `placed` is sorted by `timeMs`, stable by input order.
 */
export function placeMarkers(
  specs: MarkerSpec[],
  barBuckets: number[],
  interval: IntervalSec,
): { placed: MarkerRender[]; pending: number } {
  const placed: MarkerRender[] = [];
  let pending = 0;
  for (const m of specs) {
    const target = bucketOf(m.time_ms, interval);
    let lo = 0;
    let hi = barBuckets.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (barBuckets[mid]! < target) lo = mid + 1;
      else hi = mid;
    }
    if (lo === barBuckets.length) {
      pending++;
      continue;
    }
    placed.push({ id: m.id, timeMs: barBuckets[lo]!, position: m.position, shape: m.shape, color: m.color, text: m.text });
  }
  placed.sort((a, b) => a.timeMs - b.timeMs);
  return { placed, pending };
}
