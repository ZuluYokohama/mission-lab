import type { BarCols } from "../protocol/types";
import { bucketOf, type IntervalSec } from "../time";

export interface Bar {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  vol: number;
}

/** A bucket with no data (renders as a gap). */
export interface Whitespace {
  t: number;
}

export type CandleItem = Bar | Whitespace;

export function isBar(x: CandleItem): x is Bar {
  return "o" in x;
}

const INITIAL_CAPACITY = 1024;

/**
 * Append-mostly store of 1-second bars in growable typed arrays (strictly
 * ascending `t`). Higher intervals are derived by aggregation on demand; the
 * live bucket is re-aggregated from only its own 1s bars via binary search.
 */
export class BarStore {
  private cap = INITIAL_CAPACITY;
  private n = 0;
  private t = new Float64Array(this.cap);
  private o = new Float64Array(this.cap);
  private h = new Float64Array(this.cap);
  private l = new Float64Array(this.cap);
  private c = new Float64Array(this.cap);
  private vol = new Float64Array(this.cap);

  get size(): number {
    return this.n;
  }

  /** Replace all state. Assumes `cols.t` is strictly ascending. */
  reset(cols: BarCols): void {
    const len = cols.t.length;
    this.cap = Math.max(INITIAL_CAPACITY, len);
    this.t = Float64Array.from(cols.t);
    this.o = Float64Array.from(cols.o);
    this.h = Float64Array.from(cols.h);
    this.l = Float64Array.from(cols.l);
    this.c = Float64Array.from(cols.c);
    this.vol = Float64Array.from(cols.vol);
    this.n = len;
    this.grow(this.cap);
  }

  applyBar(bar: Bar): "appended" | "replaced" | "stale" {
    const n = this.n;
    if (n > 0) {
      const last = this.t[n - 1]!;
      if (bar.t < last) return "stale";
      if (bar.t === last) {
        this.write(n - 1, bar);
        return "replaced";
      }
    }
    if (n === this.cap) this.grow(this.cap * 2);
    this.write(n, bar);
    this.n = n + 1;
    return "appended";
  }

  /** All buckets of `interval` that contain at least one 1s bar, ascending. */
  aggregate(interval: IntervalSec): Bar[] {
    const out: Bar[] = [];
    const { t, o, h, l, c, vol, n } = this;
    let cur: Bar | undefined;
    for (let i = 0; i < n; i++) {
      const b = bucketOf(t[i]!, interval);
      if (cur === undefined || cur.t !== b) {
        cur = { t: b, o: o[i]!, h: h[i]!, l: l[i]!, c: c[i]!, vol: vol[i]! };
        out.push(cur);
      } else {
        if (h[i]! > cur.h) cur.h = h[i]!;
        if (l[i]! < cur.l) cur.l = l[i]!;
        cur.c = c[i]!;
        cur.vol += vol[i]!;
      }
    }
    return out;
  }

  /** The aggregated bar for one bucket, or undefined if it holds no 1s bars. */
  bucketBar(interval: IntervalSec, bucketMs: number): Bar | undefined {
    const lo = this.lowerBound(bucketMs);
    const end = bucketMs + interval * 1000;
    if (lo >= this.n || this.t[lo]! >= end) return undefined;
    const bar: Bar = { t: bucketMs, o: this.o[lo]!, h: this.h[lo]!, l: this.l[lo]!, c: this.c[lo]!, vol: this.vol[lo]! };
    for (let i = lo + 1; i < this.n && this.t[i]! < end; i++) {
      if (this.h[i]! > bar.h) bar.h = this.h[i]!;
      if (this.l[i]! < bar.l) bar.l = this.l[i]!;
      bar.c = this.c[i]!;
      bar.vol += this.vol[i]!;
    }
    return bar;
  }

  /** Ascending bucket starts of `interval` that contain data. */
  barBuckets(interval: IntervalSec): number[] {
    const out: number[] = [];
    for (let i = 0; i < this.n; i++) {
      const b = bucketOf(this.t[i]!, interval);
      if (out.length === 0 || out[out.length - 1] !== b) out.push(b);
    }
    return out;
  }

  firstMs(): number | null {
    return this.n > 0 ? this.t[0]! : null;
  }

  lastMs(): number | null {
    return this.n > 0 ? this.t[this.n - 1]! : null;
  }

  /** Drop 1s bars with `t < tMs`. */
  trimBefore(tMs: number): void {
    const k = this.lowerBound(tMs);
    if (k === 0) return;
    const n = this.n - k;
    for (const a of [this.t, this.o, this.h, this.l, this.c, this.vol]) a.copyWithin(0, k, this.n);
    this.n = n;
  }

  private write(i: number, b: Bar): void {
    this.t[i] = b.t;
    this.o[i] = b.o;
    this.h[i] = b.h;
    this.l[i] = b.l;
    this.c[i] = b.c;
    this.vol[i] = b.vol;
  }

  private grow(cap: number): void {
    const next = (a: Float64Array<ArrayBuffer>): Float64Array<ArrayBuffer> => {
      if (a.length === cap) return a;
      const b = new Float64Array(cap);
      b.set(a.subarray(0, this.n));
      return b;
    };
    this.t = next(this.t);
    this.o = next(this.o);
    this.h = next(this.h);
    this.l = next(this.l);
    this.c = next(this.c);
    this.vol = next(this.vol);
    this.cap = cap;
  }

  /** First index whose `t >= tMs` (n if none). */
  private lowerBound(tMs: number): number {
    let lo = 0;
    let hi = this.n;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (this.t[mid]! < tMs) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }
}

/**
 * Dense candle grid: one item per `interval` bucket from `bucketOf(fromMs)` to
 * `bucketOf(toMs)` inclusive, a bar where `bars` has that bucket, else whitespace.
 * `bars` must be ascending by `t` on bucket boundaries (as `aggregate` returns).
 */
export function densify(bars: Bar[], interval: IntervalSec, fromMs: number, toMs: number): CandleItem[] {
  if (fromMs > toMs) return [];
  const step = interval * 1000;
  const first = bucketOf(fromMs, interval);
  const last = bucketOf(toMs, interval);
  const out: CandleItem[] = [];
  let j = 0;
  while (j < bars.length && bars[j]!.t < first) j++;
  for (let t = first; t <= last; t += step) {
    if (j < bars.length && bars[j]!.t === t) out.push({ ...bars[j++]! });
    else out.push({ t });
  }
  return out;
}
