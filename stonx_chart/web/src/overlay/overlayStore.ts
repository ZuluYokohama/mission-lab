/**
 * Holds the server-defined overlays and their series points. Every per-id collection is a `Map`
 * (the decoder accepts ids such as "__proto__"). Upserting an id that already exists replaces its spec
 * in place (Map insertion order is kept), so `groups()` and `markers()` order is the first-upsert order.
 */
import type { BandSpec, LevelSpec, MarkerSpec, OverlaySpec, PaneSpec, PointCols, SeriesSpec } from "../protocol/types";
import type { IntervalSec } from "../time";
import { aggregatePoints, bandRender, bucketValue, type BandRender, type Point } from "./bucket";

/** First index of ascending `t` whose value is `>= x` (t.length if none). */
function lowerBound(t: number[], x: number): number {
  let lo = 0;
  let hi = t.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (t[mid]! < x) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export type UpsertOutcome = "applied" | "held" | "ignored";

const PRICE = "price";

export class OverlayStore {
  /** Applied specs in first-upsert order. */
  private readonly applied = new Map<string, OverlaySpec>();
  /** Specs waiting for their pane to arrive. */
  private readonly held = new Map<string, OverlaySpec>();
  /** Point columns of every series that ever had a spec upserted (applied or held). */
  private readonly pts = new Map<string, PointCols>();

  /**
   * Replace all state with a snapshot. Every held overlay is dropped: those held before the reset,
   * and those of the snapshot itself whose pane is still missing once all specs are in (all counted).
   */
  reset(specs: OverlaySpec[], points: Record<string, PointCols>): { droppedHeld: number } {
    const heldBefore = this.held.size;
    this.applied.clear();
    this.held.clear();
    this.pts.clear();
    for (const spec of specs) this.upsert(spec);
    for (const [id, cols] of Object.entries(points)) {
      const mine = this.pts.get(id);
      if (!mine) continue;
      const n = Math.min(cols.t.length, cols.v.length);
      mine.t = cols.t.slice(0, n);
      mine.v = cols.v.slice(0, n);
    }
    const droppedHeld = heldBefore + this.held.size;
    for (const id of this.held.keys()) this.pts.delete(id);
    this.held.clear();
    return { droppedHeld };
  }

  upsert(spec: OverlaySpec): UpsertOutcome {
    const copy = { ...spec };
    if (copy.kind === "marker" && copy.pane !== PRICE) return "ignored";

    const placeable = copy.kind === "pane" || copy.pane === PRICE || this.isPane(copy.pane);
    if (placeable) {
      this.held.delete(copy.id);
      this.applied.set(copy.id, copy);
    } else {
      this.applied.delete(copy.id);
      this.held.set(copy.id, copy);
    }

    if (copy.kind === "series") {
      if (!this.pts.has(copy.id)) this.pts.set(copy.id, { t: [], v: [] });
    } else {
      this.pts.delete(copy.id);
    }

    if (copy.kind === "pane") this.release(copy.id);
    return placeable ? "applied" : "held";
  }

  remove(id: string): boolean {
    const had = this.applied.delete(id) || this.held.delete(id);
    this.pts.delete(id);
    return had;
  }

  appendPoints(id: string, t: number[], v: number[]): { touched: number[]; rejected: number } {
    const cols = this.pts.get(id);
    if (!cols) return { touched: [], rejected: t.length };
    const n = Math.min(t.length, v.length);
    const touched: number[] = [];
    let rejected = t.length - n;
    for (let i = 0; i < n; i++) {
      const ti = t[i]!;
      const vi = v[i]!;
      if (!Number.isFinite(ti) || !Number.isFinite(vi)) {
        rejected++;
        continue;
      }
      const last = cols.t.length - 1;
      if (last < 0 || ti > cols.t[last]!) {
        cols.t.push(ti);
        cols.v.push(vi);
      } else if (ti === cols.t[last]) {
        cols.v[last] = vi;
      } else {
        rejected++;
        continue;
      }
      touched.push(ti);
    }
    return { touched, rejected };
  }

  panes(): PaneSpec[] {
    return this.ofKind<PaneSpec>("pane").sort((a, b) => a.order - b.order);
  }

  series(): SeriesSpec[] {
    return this.ofKind<SeriesSpec>("series");
  }

  levels(): LevelSpec[] {
    return this.ofKind<LevelSpec>("level");
  }

  /** Applied bands bucketed at `interval`, optionally only those whose spec passes `include`. */
  bands(interval: IntervalSec, include?: (spec: BandSpec) => boolean): BandRender[] {
    const out: BandRender[] = [];
    for (const spec of this.applied.values()) {
      if (spec.kind === "band" && (include === undefined || include(spec))) out.push(bandRender(spec, interval));
    }
    return out;
  }

  markers(): MarkerSpec[] {
    return this.ofKind<MarkerSpec>("marker");
  }

  /** A copy of the series' point columns, or undefined when no series with that id was ever upserted. */
  points(id: string): PointCols | undefined {
    const cols = this.pts.get(id);
    return cols ? { t: cols.t.slice(), v: cols.v.slice() } : undefined;
  }

  /**
   * Bucketed data of an applied series at `interval`, read straight from the stored columns (no copy of
   * the raw points). Empty for an unknown or held series.
   */
  seriesData(id: string, interval: IntervalSec): Point[] {
    const spec = this.applied.get(id);
    const cols = this.pts.get(id);
    if (spec?.kind !== "series" || !cols) return [];
    return aggregatePoints(cols, interval, spec.agg);
  }

  /**
   * Value of one bucket of an applied series; undefined when it has no points there. The live (last)
   * bucket folds in place in O(points in bucket); an older bucket is located by binary search and only
   * its own points are copied.
   */
  seriesBucketValue(id: string, interval: IntervalSec, bucketMs: number): number | undefined {
    const spec = this.applied.get(id);
    const cols = this.pts.get(id);
    if (spec?.kind !== "series" || !cols) return undefined;
    const endMs = bucketMs + interval * 1000;
    const n = cols.t.length;
    if (n === 0 || cols.t[n - 1]! < endMs) return bucketValue(cols, interval, spec.agg, bucketMs);
    const lo = lowerBound(cols.t, bucketMs);
    const hi = lowerBound(cols.t, endMs);
    if (lo === hi) return undefined;
    return bucketValue({ t: cols.t.slice(lo, hi), v: cols.v.slice(lo, hi) }, interval, spec.agg, bucketMs);
  }

  /** First and last point time of one series (applied or held), or null when it has no points. */
  seriesExtent(id: string): { first: number; last: number } | null {
    const cols = this.pts.get(id);
    if (!cols || cols.t.length === 0) return null;
    return { first: cols.t[0]!, last: cols.t[cols.t.length - 1]! };
  }

  /** Kind of the applied or held spec with this id. */
  kindOf(id: string): OverlaySpec["kind"] | undefined {
    return (this.applied.get(id) ?? this.held.get(id))?.kind;
  }

  /** Earliest and latest point time over all applied series, or null when there are no points. */
  pointsExtent(): { first: number; last: number } | null {
    let first = Infinity;
    let last = -Infinity;
    for (const [id, cols] of this.pts) {
      if (this.held.has(id) || cols.t.length === 0) continue;
      first = Math.min(first, cols.t[0]!);
      last = Math.max(last, cols.t[cols.t.length - 1]!);
    }
    return first === Infinity ? null : { first, last };
  }

  /** Distinct groups of applied specs, in first-seen order. */
  groups(): string[] {
    const seen = new Set<string>();
    for (const spec of this.applied.values()) seen.add(spec.group);
    return [...seen];
  }

  /** Drop points before `tMs`, bands closed at or before it, and markers before it. */
  trimBefore(tMs: number): void {
    for (const cols of this.pts.values()) {
      let lo = 0;
      let hi = cols.t.length;
      while (lo < hi) {
        const mid = (lo + hi) >>> 1;
        if (cols.t[mid]! < tMs) lo = mid + 1;
        else hi = mid;
      }
      if (lo > 0) {
        cols.t.splice(0, lo);
        cols.v.splice(0, lo);
      }
    }
    for (const map of [this.applied, this.held]) {
      for (const [id, spec] of map) {
        if ((spec.kind === "band" && spec.end_ms !== null && spec.end_ms <= tMs) || (spec.kind === "marker" && spec.time_ms < tMs)) {
          map.delete(id);
        }
      }
    }
  }

  private isPane(id: string): boolean {
    return this.applied.get(id)?.kind === "pane";
  }

  /** Move held specs waiting on pane `paneId` into the applied set, in held order. */
  private release(paneId: string): void {
    for (const [id, spec] of this.held) {
      if (spec.pane !== paneId) continue;
      this.held.delete(id);
      this.applied.set(id, spec);
    }
  }

  private ofKind<T extends OverlaySpec>(kind: T["kind"]): T[] {
    const out: T[] = [];
    for (const spec of this.applied.values()) if (spec.kind === kind) out.push({ ...spec } as T);
    return out;
  }
}
