/**
 * Turns decoded stream messages into store mutations and applies the accumulated changes to a
 * `ChartSurface` at most once per `flush()` (one per animation frame). `handle` never touches the
 * surface. A flush is either a full reset (snapshot, interval change, trim, visibility toggle, or a
 * change the incremental path cannot express) or an incremental update bounded by distinct buckets.
 */
import { BarStore, densify, type Bar, type CandleItem } from "../bars/barStore";
import { placeMarkers, type BandRender, type MarkerRender } from "../overlay/bucket";
import { OverlayStore } from "../overlay/overlayStore";
import type { LevelSpec, OverlaySpec, PaneSpec, SeriesSpec, StreamMessage } from "../protocol/types";
import type { ChartSurface } from "../surface/types";
import { bucketOf, type IntervalSec } from "../time";

export const WINDOW_MS = 23_400_000;

/** Trim cutoffs are aligned to the largest interval so no interval gets a partial first bucket. */
const TRIM_ALIGN: IntervalSec = 300;
const PRICE = "price";
const DEFAULT_INTERVAL: IntervalSec = 60;

export interface Counters {
  malformed: number;
  ignored: number;
  gaps: number;
  stale: number;
}

export interface Readout {
  bar: Bar | null;
  values: { id: string; group: string; value: number }[];
}

export interface ControllerState {
  asset: string | null;
  assets: string[];
  source: string | null;
  interval: IntervalSec;
  groups: string[];
  hiddenGroups: string[];
  feedState: "ok" | "degraded";
  dropCount: number;
  statusMessage: string | null;
  lastMessageAtMs: number | null;
  barCount: number;
  /** A copy of the decode/sequence counters (malformed, ignored, gaps, stale). */
  counters: Counters;
}

export interface ControllerOptions {
  now?: () => number;
  windowMs?: number;
  interval?: IntervalSec;
  hiddenGroups?: string[];
}

/** What is visible at the moment, derived from the stores and the hidden groups. */
interface Model {
  /** Every applied series, visible or not (their points span the candle grid). */
  allSeries: SeriesSpec[];
  series: SeriesSpec[];
  panes: PaneSpec[];
  /** `price` plus the ids of `panes`. */
  shown: Set<string>;
}

const paneKey = (panes: PaneSpec[]): string => JSON.stringify(panes);
const bandKey = (bands: BandRender[]): string => JSON.stringify(bands);

export class ChartController {
  readonly counters: Counters = { malformed: 0, ignored: 0, gaps: 0, stale: 0 };

  private readonly bars = new BarStore();
  private readonly overlays = new OverlayStore();
  private readonly now: () => number;
  private readonly windowMs: number;
  private interval: IntervalSec;
  private readonly hidden: Set<string>;

  private asset: string | null = null;
  private assets: string[] = [];
  private source: string | null = null;
  private feedState: "ok" | "degraded" = "ok";
  private dropCount = 0;
  private statusMessage: string | null = null;
  private lastMessageAtMs: number | null = null;
  private expectedSeq: number | null = null;

  // Dirty state, cleared by every flush.
  private fullDirty = false;
  /** Buckets (at the current interval) that received a 1s bar. */
  private readonly dirtyBars = new Set<number>();
  /** Per series id: ascending buckets (at the current interval) that received a point. */
  private readonly dirtyPoints = new Map<string, number[]>();
  private structuralDirty = false;
  private readonly upsertedSeries = new Set<string>();
  private levelsDirty = false;
  private bandsDirty = false;
  private markersDirty = false;

  // Mirror of what the surface shows.
  private rendered = false;
  private gridFirst: number | null = null;
  private gridLast: number | null = null;
  /** Ascending buckets holding a real bar at the current interval; its length is the bar count. */
  private barBuckets: number[] = [];
  private readonly renderedSeries = new Map<string, SeriesSpec>();
  /** Ids of every applied series as of the last flush (the series whose points span the grid). */
  private appliedSeries = new Set<string>();
  private sentPanes = "";
  private sentBands = "";
  private pendingMarkers = 0;

  constructor(
    private readonly surface: ChartSurface,
    opts: ControllerOptions = {},
  ) {
    this.now = opts.now ?? Date.now;
    this.windowMs = opts.windowMs ?? WINDOW_MS;
    this.interval = opts.interval ?? DEFAULT_INTERVAL;
    this.hidden = new Set(opts.hiddenGroups ?? []);
  }

  /** A new connection: its first message sets the expected sequence number. */
  connectionOpened(): void {
    this.expectedSeq = null;
  }

  noteDecodeIssue(reason: "malformed" | "ignored"): void {
    this.lastMessageAtMs = this.now();
    this.counters[reason]++;
  }

  handle(msg: StreamMessage): void {
    this.lastMessageAtMs = this.now();
    if (this.expectedSeq !== null && msg.seq !== this.expectedSeq) this.counters.gaps++;
    this.expectedSeq = msg.seq + 1;
    this.source = msg.source;

    switch (msg.type) {
      case "snapshot": {
        this.bars.reset(msg.bars);
        this.counters.ignored += this.overlays.reset(msg.overlays, msg.points).droppedHeld;
        this.asset = msg.asset;
        this.assets = msg.assets.map((a) => a.symbol);
        this.markFull();
        this.maybeTrim();
        return;
      }
      case "bar": {
        const outcome = this.bars.applyBar({ t: msg.t, o: msg.o, h: msg.h, l: msg.l, c: msg.c, vol: msg.vol });
        if (outcome === "stale") {
          this.counters.stale++;
          return;
        }
        if (!this.fullDirty) this.dirtyBars.add(bucketOf(msg.t, this.interval));
        this.maybeTrim();
        return;
      }
      case "series.points": {
        const { touched, rejected } = this.overlays.appendPoints(msg.id, msg.points.t, msg.points.v);
        this.counters.ignored += rejected;
        if (touched.length === 0) return;
        if (!this.fullDirty) {
          let list = this.dirtyPoints.get(msg.id);
          if (!list) this.dirtyPoints.set(msg.id, (list = []));
          for (const t of touched) {
            const b = bucketOf(t, this.interval);
            if (list[list.length - 1] !== b) list.push(b);
          }
        }
        this.maybeTrim();
        return;
      }
      case "overlay.upsert": {
        const before = this.overlays.kindOf(msg.overlay.id);
        if (this.overlays.upsert(msg.overlay) === "ignored") {
          this.counters.ignored++;
          return;
        }
        if (before !== undefined && before !== msg.overlay.kind) this.markKind(before, msg.overlay.id);
        this.markKind(msg.overlay.kind, msg.overlay.id);
        return;
      }
      case "overlay.remove": {
        const kind = this.overlays.kindOf(msg.id);
        if (kind !== undefined && this.overlays.remove(msg.id)) this.markKind(kind, msg.id);
        return;
      }
      case "heartbeat":
        return;
      case "status":
        this.feedState = msg.feed_state === "ok" ? "ok" : "degraded";
        this.dropCount = msg.drop_count;
        this.statusMessage = msg.message ?? null;
        return;
    }
  }

  setInterval(i: IntervalSec): void {
    if (i === this.interval) return;
    this.interval = i;
    this.markFull();
  }

  setGroupVisible(group: string, visible: boolean): void {
    if (visible === !this.hidden.has(group)) return;
    if (visible) this.hidden.delete(group);
    else this.hidden.add(group);
    this.markFull();
  }

  flush(): { fullReset: boolean } {
    const dirty = this.anyDirty();
    if (this.fullDirty || (!this.rendered && dirty)) {
      this.fullRender();
      return { fullReset: true };
    }
    if (!dirty) return { fullReset: false };
    return this.incremental();
  }

  readoutAt(tMs: number): Readout | null {
    const b = bucketOf(tMs, this.interval);
    const bar = this.bars.bucketBar(this.interval, b) ?? null;
    const values: Readout["values"] = [];
    for (const [id, spec] of this.renderedSeries) {
      const value = this.overlays.seriesBucketValue(id, this.interval, b);
      if (value !== undefined) values.push({ id, group: spec.group, value });
    }
    return bar === null && values.length === 0 ? null : { bar, values };
  }

  state(): ControllerState {
    return {
      asset: this.asset,
      assets: [...this.assets],
      source: this.source,
      interval: this.interval,
      groups: this.overlays.groups(),
      hiddenGroups: [...this.hidden],
      feedState: this.feedState,
      dropCount: this.dropCount,
      statusMessage: this.statusMessage,
      lastMessageAtMs: this.lastMessageAtMs,
      barCount: this.barBuckets.length,
      counters: { ...this.counters },
    };
  }

  // --- handling helpers ---

  private markFull(): void {
    this.fullDirty = true;
    this.clearIncremental();
  }

  private markKind(kind: OverlaySpec["kind"], id: string): void {
    if (this.fullDirty) return;
    switch (kind) {
      case "pane":
        // A pane can release held series, levels and bands and changes which panes are visible.
        this.structuralDirty = true;
        this.levelsDirty = true;
        this.bandsDirty = true;
        return;
      case "series":
        this.structuralDirty = true;
        this.upsertedSeries.add(id);
        return;
      case "level":
        this.levelsDirty = true;
        return;
      case "band":
        this.bandsDirty = true;
        return;
      case "marker":
        this.markersDirty = true;
        return;
    }
  }

  /** When the data spans more than 110% of the window, cut at a 300 s boundary `windowMs` before the end. */
  private maybeTrim(): void {
    const ext = this.dataExtent();
    if (ext === null) return;
    // Integer form of `span > 1.1 × windowMs` (1.1 × 23_400_000 is not exact in floating point).
    if ((ext.last - ext.first) * 10 <= this.windowMs * 11) return;
    const cut = bucketOf(ext.last - this.windowMs, TRIM_ALIGN);
    if (cut <= ext.first) return; // nothing would be dropped (only with windows under ~300 s)
    this.bars.trimBefore(cut);
    this.overlays.trimBefore(cut);
    this.markFull();
  }

  /** First and last time over the bars and every applied series' points. */
  private dataExtent(): { first: number; last: number } | null {
    const bf = this.bars.firstMs();
    const bl = this.bars.lastMs();
    const pe = this.overlays.pointsExtent();
    if (bf === null || bl === null) return pe;
    if (pe === null) return { first: bf, last: bl };
    return { first: Math.min(bf, pe.first), last: Math.max(bl, pe.last) };
  }

  private anyDirty(): boolean {
    return (
      this.dirtyBars.size > 0 ||
      this.dirtyPoints.size > 0 ||
      this.structuralDirty ||
      this.levelsDirty ||
      this.bandsDirty ||
      this.markersDirty
    );
  }

  private clearIncremental(): void {
    this.dirtyBars.clear();
    this.dirtyPoints.clear();
    this.structuralDirty = false;
    this.upsertedSeries.clear();
    this.levelsDirty = false;
    this.bandsDirty = false;
    this.markersDirty = false;
  }

  // --- visibility ---

  private model(): Model {
    const hidden = this.hidden;
    const panes = this.overlays.panes();
    const paneById = new Map(panes.map((p) => [p.id, p]));
    const paneOk = (id: string): boolean => {
      if (id === PRICE) return true;
      const p = paneById.get(id);
      return p !== undefined && !hidden.has(p.group);
    };
    const allSeries = this.overlays.series();
    const series = allSeries.filter((s) => !hidden.has(s.group) && paneOk(s.pane));
    const hosting = new Set(series.map((s) => s.pane));
    const visible = panes.filter((p) => !hidden.has(p.group) && hosting.has(p.id));
    return { allSeries, series, panes: visible, shown: new Set([PRICE, ...visible.map((p) => p.id)]) };
  }

  private visibleLevels(m: Model): LevelSpec[] {
    return this.overlays.levels().filter((l) => !this.hidden.has(l.group) && m.shown.has(l.pane));
  }

  private visibleBands(m: Model): BandRender[] {
    return this.overlays.bands(this.interval, (b) => !this.hidden.has(b.group) && m.shown.has(b.pane));
  }

  private placedMarkers(): { placed: MarkerRender[]; pending: number } {
    const specs = this.overlays.markers().filter((s) => !this.hidden.has(s.group));
    return placeMarkers(specs, this.barBuckets, this.interval);
  }

  // --- flushing ---

  private fullRender(): void {
    const s = this.surface;
    const I = this.interval;
    const wasLive = s.isAtLiveEdge();
    const range = s.getVisibleRange();

    const agg = this.bars.aggregate(I);
    const ext = this.dataExtent();
    s.setCandles(ext === null ? [] : densify(agg, I, ext.first, ext.last));
    this.gridFirst = ext === null ? null : bucketOf(ext.first, I);
    this.gridLast = ext === null ? null : bucketOf(ext.last, I);
    this.barBuckets = agg.map((b) => b.t);

    const m = this.model();
    const visibleIds = new Set(m.series.map((x) => x.id));
    for (const id of [...this.renderedSeries.keys()]) {
      if (visibleIds.has(id)) continue;
      s.removeSeries(id);
      this.renderedSeries.delete(id);
    }
    s.setPanes(m.panes);
    this.sentPanes = paneKey(m.panes);
    for (const spec of m.series) {
      s.setSeries(spec, this.overlays.seriesData(spec.id, I));
      this.renderedSeries.set(spec.id, spec);
    }
    this.appliedSeries = new Set(m.allSeries.map((x) => x.id));
    s.setLevels(this.visibleLevels(m));
    const bands = this.visibleBands(m);
    s.setBands(bands);
    this.sentBands = bandKey(bands);
    const markers = this.placedMarkers();
    s.setMarkers(markers.placed);
    this.pendingMarkers = markers.pending;

    if (wasLive) s.scrollToLive();
    else if (range !== null) s.setVisibleRange(range);

    this.rendered = true;
    this.fullDirty = false;
    this.clearIncremental();
  }

  /** True when the pending changes cannot be applied as appends to the current grid. */
  private needsFullReset(m: Model | null): boolean {
    const first = this.gridFirst;
    const last = this.gridLast;
    for (const b of this.dirtyBars) if (last === null || b < last) return true;
    for (const [id, buckets] of this.dirtyPoints) {
      if (!this.appliedSeries.has(id)) continue;
      if (first === null || buckets[0]! < first) return true;
    }
    if (m !== null) {
      // A series that became applied (a pane released it) may hold points outside the grid.
      for (const spec of m.allSeries) {
        if (this.appliedSeries.has(spec.id)) continue;
        const ext = this.overlays.seriesExtent(spec.id);
        if (ext === null) continue;
        if (first === null || last === null) return true;
        if (bucketOf(ext.first, this.interval) < first || bucketOf(ext.last, this.interval) > last) return true;
      }
    }
    return false;
  }

  private incremental(): { fullReset: boolean } {
    const s = this.surface;
    const I = this.interval;
    const step = I * 1000;
    const m = this.structuralDirty || this.levelsDirty || this.bandsDirty ? this.model() : null;

    if (this.needsFullReset(this.structuralDirty ? m : null)) {
      this.fullRender();
      return { fullReset: true };
    }

    // 1. Candles: the last bucket's bar, then new buckets (whitespace for skipped ones).
    const last = this.gridLast!;
    if (this.structuralDirty && m !== null) this.appliedSeries = new Set(m.allSeries.map((x) => x.id));
    const fresh = new Set<number>();
    for (const b of this.dirtyBars) if (b > last) fresh.add(b);
    for (const [id, buckets] of this.dirtyPoints) {
      if (!this.appliedSeries.has(id)) continue;
      for (let i = buckets.length - 1; i >= 0 && buckets[i]! > last; i--) fresh.add(buckets[i]!);
    }
    if (this.dirtyBars.has(last)) s.updateCandle(this.candleAt(last));
    let prev = last;
    for (const b of [...fresh].sort((x, y) => x - y)) {
      for (let w = prev + step; w < b; w += step) s.updateCandle({ t: w });
      s.updateCandle(this.dirtyBars.has(b) ? this.candleAt(b) : { t: b });
      prev = b;
    }
    this.gridLast = prev;

    let newBarBucket = false;
    for (const b of [...this.dirtyBars].sort((x, y) => x - y)) {
      const lastBar = this.barBuckets[this.barBuckets.length - 1];
      if (lastBar === undefined || b > lastBar) {
        this.barBuckets.push(b);
        newBarBucket = true;
      }
    }

    // Plan structural changes first so point updates skip series that are being replaced or removed.
    const toRemove: string[] = [];
    const toSet: SeriesSpec[] = [];
    let panesChanged = false;
    if (this.structuralDirty && m !== null) {
      const visibleIds = new Set(m.series.map((x) => x.id));
      for (const id of this.renderedSeries.keys()) if (!visibleIds.has(id)) toRemove.push(id);
      for (const spec of m.series) {
        if (!this.renderedSeries.has(spec.id) || this.upsertedSeries.has(spec.id)) toSet.push(spec);
      }
      panesChanged = paneKey(m.panes) !== this.sentPanes;
    }
    const skip = new Set([...toRemove, ...toSet.map((x) => x.id)]);

    // 2. Series points, per series in ascending bucket order.
    for (const [id, buckets] of this.dirtyPoints) {
      if (!this.renderedSeries.has(id) || skip.has(id)) continue;
      for (const b of buckets) {
        const v = this.overlays.seriesBucketValue(id, I, b);
        if (v !== undefined) s.updateSeriesPoint(id, { t: b, v });
      }
    }

    // 3. Structure: series removals, panes, series (re)creations, levels.
    for (const id of toRemove) {
      s.removeSeries(id);
      this.renderedSeries.delete(id);
    }
    if (panesChanged && m !== null) {
      s.setPanes(m.panes);
      this.sentPanes = paneKey(m.panes);
    }
    for (const spec of toSet) {
      s.setSeries(spec, this.overlays.seriesData(spec.id, I));
      this.renderedSeries.set(spec.id, spec);
    }
    if ((this.levelsDirty || panesChanged) && m !== null) s.setLevels(this.visibleLevels(m));

    // 4. Bands, when what would be shown differs from what was sent.
    if ((this.bandsDirty || panesChanged) && m !== null) {
      const bands = this.visibleBands(m);
      const key = bandKey(bands);
      if (key !== this.sentBands) {
        s.setBands(bands);
        this.sentBands = key;
      }
    }

    // 5. Markers: on marker changes, or when a new bar bucket may have reached a pending marker.
    if (this.markersDirty || (this.pendingMarkers > 0 && newBarBucket)) {
      const markers = this.placedMarkers();
      if (this.markersDirty || markers.pending !== this.pendingMarkers) s.setMarkers(markers.placed);
      this.pendingMarkers = markers.pending;
    }

    this.clearIncremental();
    return { fullReset: false };
  }

  private candleAt(bucketMs: number): CandleItem {
    return this.bars.bucketBar(this.interval, bucketMs) ?? { t: bucketMs };
  }
}
