/**
 * Test double for `ChartSurface`: records every call (queries included) in `calls` and keeps a model
 * of what a real chart would show. Updates older than the last item throw, as Lightweight Charts does.
 */
import type { CandleItem } from "../bars/barStore";
import type { BandRender, MarkerRender, Point } from "../overlay/bucket";
import type { LevelSpec, PaneSpec, SeriesSpec } from "../protocol/types";
import type { ChartSurface, VisibleRange } from "./types";

export interface SurfaceCall {
  method: string;
  args: unknown[];
}

/** Replace the last item when `t` matches it, append when newer, throw when older. */
function upsertLast<T extends { t: number }>(items: T[], item: T, what: string): void {
  const last = items[items.length - 1];
  if (last === undefined || item.t > last.t) items.push(item);
  else if (item.t === last.t) items[items.length - 1] = item;
  else throw new Error(`${what}: update at ${item.t} is older than the last item at ${last.t}`);
}

export class FakeSurface implements ChartSurface {
  readonly calls: SurfaceCall[] = [];

  candles: CandleItem[] = [];
  readonly series = new Map<string, { spec: SeriesSpec; data: Point[] }>();
  panes: PaneSpec[] = [];
  levels: LevelSpec[] = [];
  bands: BandRender[] = [];
  markers: MarkerRender[] = [];

  atLiveEdge = true;
  visibleRange: VisibleRange | null = null;
  dimmed = false;
  destroyed = false;

  private readonly crosshairCbs: ((tMs: number | null) => void)[] = [];
  private readonly viewCbs: (() => void)[] = [];

  setCandles(items: CandleItem[]): void {
    this.record("setCandles", items);
    this.candles = items.map((c) => ({ ...c }));
  }

  updateCandle(item: CandleItem): void {
    this.record("updateCandle", item);
    upsertLast(this.candles, { ...item }, "updateCandle");
  }

  setPanes(panes: PaneSpec[]): void {
    this.record("setPanes", panes);
    this.panes = panes.map((p) => ({ ...p }));
  }

  setSeries(spec: SeriesSpec, data: Point[]): void {
    this.record("setSeries", spec, data);
    this.series.set(spec.id, { spec: { ...spec }, data: data.map((p) => ({ ...p })) });
  }

  updateSeriesPoint(id: string, p: Point): void {
    this.record("updateSeriesPoint", id, p);
    const s = this.series.get(id);
    if (!s) throw new Error(`updateSeriesPoint: unknown series ${id}`);
    upsertLast(s.data, { ...p }, `updateSeriesPoint(${id})`);
  }

  removeSeries(id: string): void {
    this.record("removeSeries", id);
    if (!this.series.delete(id)) throw new Error(`removeSeries: unknown series ${id}`);
  }

  setLevels(levels: LevelSpec[]): void {
    this.record("setLevels", levels);
    this.levels = levels.map((l) => ({ ...l }));
  }

  setBands(bands: BandRender[]): void {
    this.record("setBands", bands);
    this.bands = bands.map((b) => ({ ...b }));
  }

  setMarkers(markers: MarkerRender[]): void {
    this.record("setMarkers", markers);
    this.markers = markers.map((m) => ({ ...m }));
  }

  getVisibleRange(): VisibleRange | null {
    this.record("getVisibleRange");
    return this.visibleRange ? { ...this.visibleRange } : null;
  }

  setVisibleRange(r: VisibleRange): void {
    this.record("setVisibleRange", r);
    this.visibleRange = { ...r };
  }

  isAtLiveEdge(): boolean {
    this.record("isAtLiveEdge");
    return this.atLiveEdge;
  }

  scrollToLive(): void {
    this.record("scrollToLive");
    this.atLiveEdge = true;
  }

  setDimmed(d: boolean): void {
    this.record("setDimmed", d);
    this.dimmed = d;
  }

  onCrosshair(cb: (tMs: number | null) => void): void {
    this.record("onCrosshair", cb);
    this.crosshairCbs.push(cb);
  }

  onViewChange(cb: () => void): void {
    this.record("onViewChange", cb);
    this.viewCbs.push(cb);
  }

  destroy(): void {
    this.record("destroy");
    this.destroyed = true;
  }

  /** Test helper: fire the crosshair callbacks as if the user hovered `tMs`. */
  emitCrosshair(tMs: number | null): void {
    for (const cb of this.crosshairCbs) cb(tMs);
  }

  /** Test helper: fire the view-change callbacks as if the user panned or zoomed. */
  emitViewChange(): void {
    for (const cb of this.viewCbs) cb();
  }

  private record(method: string, ...args: unknown[]): void {
    this.calls.push({ method, args });
  }
}
