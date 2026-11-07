/**
 * Holds the server-defined overlays and their series points. Every per-id collection is a `Map`
 * (the decoder accepts ids such as "__proto__"). Upserting an id that already exists replaces its spec
 * in place (Map insertion order is kept), so `groups()` and `markers()` order is the first-upsert order.
 */
import type { BandSpec, LevelSpec, MarkerSpec, OverlaySpec, PaneSpec, PointCols, SeriesSpec } from "../protocol/types";
import type { IntervalSec } from "../time";
import { bandRender, type BandRender } from "./bucket";

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

  bands(interval: IntervalSec): BandRender[] {
    return this.ofKind<BandSpec>("band").map((b) => bandRender(b, interval));
  }

  markers(): MarkerSpec[] {
    return this.ofKind<MarkerSpec>("marker");
  }

  /** A copy of the series' point columns, or undefined when no series with that id was ever upserted. */
  points(id: string): PointCols | undefined {
    const cols = this.pts.get(id);
    return cols ? { t: cols.t.slice(), v: cols.v.slice() } : undefined;
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
