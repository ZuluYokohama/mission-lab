/**
 * `ChartSurface` rendered with Lightweight Charts 5. The only modules that import the library are
 * `lwc*.ts`. Times cross the boundary as `(tMs / 1000) as UTCTimestamp` and are never shifted; wall
 * clock display goes through the caller's formatters.
 *
 * Pane model: pane 0 holds the candles and every `price` series; each hosted sub-pane id owns exactly
 * one chart pane, at the index `paneIndexMap` gives it. LWC deletes a pane when its last series leaves
 * and creates one when a series is added past the end, so after every add/remove/move/setPanes the
 * surface recomputes the map, puts the panes in order, sizes them and re-attaches band primitives.
 */
import {
  AreaSeries,
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  LineType,
  type CandlestickData,
  type ChartOptions,
  type CreatePriceLineOptions,
  type DeepPartial,
  type IChartApi,
  type IPaneApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineData,
  type LineWidth,
  type LogicalRangeChangeEventHandler,
  type MouseEventHandler,
  type SeriesMarker,
  type SeriesMarkerBarPosition,
  type SeriesPartialOptionsMap,
  type SeriesType,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from "lightweight-charts";
import { isBar, type CandleItem } from "../bars/barStore";
import type { BandRender, MarkerRender, Point } from "../overlay/bucket";
import type { LevelSpec, MarkerSpec, PaneSpec, SeriesSpec } from "../protocol/types";
import { BandsPrimitive } from "./lwcBands";
import { PRICE_PANE, paneIndexMap } from "./paneLayout";
import { chartColors, resolveColor, type Scheme } from "./theme";
import type { ChartSurface, VisibleRange } from "./types";

export interface LwcSurface extends ChartSurface {
  setScheme(s: Scheme): void;
  /** Number of chart panes other than the price pane (read-only; a test hook). */
  paneCount(): number;
}

export interface LwcSurfaceOptions {
  scheme: Scheme;
  formatTime: (tMs: number) => string;
  formatTick: (tMs: number) => string;
}

const MIN_SUB_PANE_PX = 30;
const MIN_PRICE_PANE_PX = 60;

const sec = (ms: number): UTCTimestamp => (ms / 1000) as UTCTimestamp;
const toMs = (t: Time): number | null => (typeof t === "number" ? t * 1000 : null);

const MARKER_POSITION: Record<MarkerSpec["position"], SeriesMarkerBarPosition> = {
  above: "aboveBar",
  below: "belowBar",
  inBar: "inBar",
};

type Entry =
  | { spec: SeriesSpec; kind: "Line"; api: ISeriesApi<"Line"> }
  | { spec: SeriesSpec; kind: "Area"; api: ISeriesApi<"Area"> }
  | { spec: SeriesSpec; kind: "Histogram"; api: ISeriesApi<"Histogram"> };

const kindOf = (style: SeriesSpec["style"]): Entry["kind"] =>
  style === "area" ? "Area" : style === "histogram" ? "Histogram" : "Line";

const lineWidth = (w: number): LineWidth =>
  (Number.isFinite(w) ? Math.min(4, Math.max(1, Math.round(w))) : 1) as LineWidth;

const commonOptions = (spec: SeriesSpec) => ({
  priceFormat: { type: "price" as const, precision: spec.precision, minMove: 10 ** -spec.precision },
  priceLineVisible: false,
});

const lineOptions = (spec: SeriesSpec, s: Scheme): SeriesPartialOptionsMap["Line"] => ({
  ...commonOptions(spec),
  color: resolveColor(spec.color, s),
  lineWidth: lineWidth(spec.width),
  lineType: spec.style === "step" ? LineType.WithSteps : LineType.Simple,
});

const areaOptions = (spec: SeriesSpec, s: Scheme): SeriesPartialOptionsMap["Area"] => ({
  ...commonOptions(spec),
  lineColor: resolveColor(spec.color, s),
  topColor: resolveColor(spec.color, s, 0.35),
  bottomColor: resolveColor(spec.color, s, 0.02),
  lineWidth: lineWidth(spec.width),
});

const histogramOptions = (spec: SeriesSpec, s: Scheme): SeriesPartialOptionsMap["Histogram"] => ({
  ...commonOptions(spec),
  color: resolveColor(spec.color, s),
});

const candleOptions = (s: Scheme): SeriesPartialOptionsMap["Candlestick"] => {
  const c = chartColors(s);
  return {
    upColor: c.up,
    downColor: c.down,
    borderUpColor: c.up,
    borderDownColor: c.down,
    wickUpColor: c.up,
    wickDownColor: c.down,
  };
};

const themeOptions = (s: Scheme): DeepPartial<ChartOptions> => {
  const c = chartColors(s);
  return {
    layout: {
      attributionLogo: true,
      background: { type: ColorType.Solid, color: c.background },
      textColor: c.text,
      panes: { separatorColor: c.grid },
    },
    grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
    rightPriceScale: { borderColor: c.grid },
  };
};

const priceLineOptions = (l: LevelSpec, s: Scheme): CreatePriceLineOptions => ({
  price: l.value,
  color: resolveColor(l.color, s),
  lineWidth: 1,
  lineStyle: l.dash ? LineStyle.Dashed : LineStyle.Solid,
  lineVisible: true,
  axisLabelVisible: true,
  title: l.label,
});

const toCandle = (c: CandleItem): CandlestickData<Time> | WhitespaceData<Time> =>
  isBar(c) ? { time: sec(c.t), open: c.o, high: c.h, low: c.l, close: c.c } : { time: sec(c.t) };

const toPoints = (data: Point[]): LineData<Time>[] => data.map((p) => ({ time: sec(p.t), value: p.v }));

interface BandSlot {
  prim: BandsPrimitive;
  bands: BandRender[];
  /** The chart pane the primitive is attached to, or null. */
  host: IPaneApi<Time> | null;
}

class LwcChartSurface implements LwcSurface {
  private readonly chart: IChartApi;
  private readonly candles: ISeriesApi<"Candlestick">;
  private readonly markersApi: ISeriesMarkersPluginApi<Time>;
  private scheme: Scheme;

  private readonly paneSpecs = new Map<string, PaneSpec>();
  private readonly series = new Map<string, Entry>();
  private indexMap = new Map<string, number>([[PRICE_PANE, 0]]);
  /** Map + heights + pane objects the heights were applied to; heights are re-applied when it changes. */
  private sizedKey = "";
  private sizedPanes: IPaneApi<Time>[] = [];
  private heightsRaf = 0;

  private levels: LevelSpec[] = [];
  private lines: ({ host: ISeriesApi<SeriesType>; line: IPriceLine } | null)[] = [];
  private markers: MarkerRender[] = [];
  private readonly bandSlots = new Map<string, BandSlot>();

  private gridFirst: number | null = null;
  private gridLast: number | null = null;

  private readonly crosshairHandlers: MouseEventHandler<Time>[] = [];
  private readonly rangeHandlers: LogicalRangeChangeEventHandler[] = [];
  private destroyed = false;

  constructor(
    private readonly container: HTMLElement,
    o: LwcSurfaceOptions,
  ) {
    this.scheme = o.scheme;
    const theme = themeOptions(o.scheme);
    this.chart = createChart(container, {
      ...theme,
      autoSize: true,
      timeScale: {
        timeVisible: true,
        secondsVisible: true,
        shiftVisibleRangeOnNewBar: true,
        // The grid often holds whitespace ahead of the last bar (series points arrive first); without
        // this a bar filling that whitespace does not shift the view and the live edge drifts away.
        allowShiftVisibleRangeOnWhitespaceReplacement: true,
        borderColor: chartColors(o.scheme).grid,
        tickMarkFormatter: (time: Time) => {
          const ms = toMs(time);
          return ms === null ? null : o.formatTick(ms);
        },
      },
      localization: {
        timeFormatter: (time: Time) => {
          const ms = toMs(time);
          return ms === null ? String(time) : o.formatTime(ms);
        },
      },
    });
    this.candles = this.chart.addSeries(CandlestickSeries, candleOptions(o.scheme), 0);
    this.markersApi = createSeriesMarkers(this.candles, []);
  }

  // --- candles ---

  setCandles(items: CandleItem[]): void {
    this.candles.setData(items.map(toCandle));
    this.gridFirst = items.length > 0 ? items[0]!.t : null;
    this.gridLast = items.length > 0 ? items[items.length - 1]!.t : null;
  }

  updateCandle(item: CandleItem): void {
    this.candles.update(toCandle(item));
    if (this.gridFirst === null) this.gridFirst = item.t;
    if (this.gridLast === null || item.t > this.gridLast) this.gridLast = item.t;
  }

  // --- panes and series ---

  setPanes(panes: PaneSpec[]): void {
    this.paneSpecs.clear();
    for (const p of panes) this.paneSpecs.set(p.id, { ...p });
    this.layout();
  }

  setSeries(spec: SeriesSpec, data: Point[]): void {
    const old = this.series.get(spec.id);
    const key = this.groupKey(spec);
    if (old !== undefined && old.kind === kindOf(spec.style)) {
      const moved = this.groupKey(old.spec) !== key;
      old.spec = { ...spec };
      if (moved) old.api.moveToPane(this.placementIndex(key, old));
      this.applyStyle(old);
      old.api.setData(toPoints(data));
    } else {
      // Add before removing the old series so a pane it shares with the replacement never empties.
      const e = this.createEntry({ ...spec }, this.placementIndex(key));
      e.api.setData(toPoints(data));
      if (old !== undefined) this.chart.removeSeries(old.api);
      this.series.set(spec.id, e);
    }
    this.layout();
  }

  updateSeriesPoint(id: string, p: Point): void {
    this.series.get(id)?.api.update({ time: sec(p.t), value: p.v });
  }

  removeSeries(id: string): void {
    const e = this.series.get(id);
    if (e === undefined) return;
    this.chart.removeSeries(e.api);
    this.series.delete(id);
    this.layout();
  }

  // --- levels, bands, markers ---

  setLevels(levels: LevelSpec[]): void {
    this.levels = levels.map((l) => ({ ...l }));
    this.syncLevels(true);
  }

  setBands(bands: BandRender[]): void {
    const byPane = new Map<string, BandRender[]>();
    for (const b of bands) {
      let list = byPane.get(b.pane);
      if (list === undefined) byPane.set(b.pane, (list = []));
      list.push({ ...b });
    }
    for (const [key, slot] of this.bandSlots) {
      if (byPane.has(key)) continue;
      this.release(slot);
      this.bandSlots.delete(key);
    }
    for (const [key, list] of byPane) {
      let slot = this.bandSlots.get(key);
      if (slot === undefined) {
        slot = { prim: new BandsPrimitive((tMs) => this.xOf(tMs)), bands: [], host: null };
        this.bandSlots.set(key, slot);
      }
      slot.bands = list;
      slot.prim.setBands(list, this.scheme);
    }
    this.syncPrimitives();
  }

  setMarkers(markers: MarkerRender[]): void {
    this.markers = markers.map((m) => ({ ...m }));
    this.applyMarkers();
  }

  // --- view ---

  getVisibleRange(): VisibleRange | null {
    const r = this.chart.timeScale().getVisibleRange();
    if (r === null) return null;
    const fromMs = toMs(r.from);
    const toMsV = toMs(r.to);
    return fromMs === null || toMsV === null ? null : { fromMs, toMs: toMsV };
  }

  setVisibleRange(r: VisibleRange): void {
    // LWC cannot map a time range onto an empty time scale (it asserts).
    if (this.gridFirst === null) return;
    this.chart.timeScale().setVisibleRange({ from: sec(r.fromMs), to: sec(r.toMs) });
  }

  isAtLiveEdge(): boolean {
    return this.chart.timeScale().scrollPosition() > -1;
  }

  scrollToLive(): void {
    this.chart.timeScale().scrollToRealTime();
  }

  setDimmed(d: boolean): void {
    this.container.style.opacity = d ? "0.5" : "";
  }

  onCrosshair(cb: (tMs: number | null) => void): void {
    const h: MouseEventHandler<Time> = (param) => cb(param.time === undefined ? null : toMs(param.time));
    this.crosshairHandlers.push(h);
    this.chart.subscribeCrosshairMove(h);
  }

  onViewChange(cb: () => void): void {
    const h: LogicalRangeChangeEventHandler = () => cb();
    this.rangeHandlers.push(h);
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(h);
  }

  setScheme(s: Scheme): void {
    this.scheme = s;
    this.chart.applyOptions({ ...themeOptions(s), timeScale: { borderColor: chartColors(s).grid } });
    this.candles.applyOptions(candleOptions(s));
    for (const e of this.series.values()) this.applyStyle(e);
    this.syncLevels(true);
    this.applyMarkers();
    for (const slot of this.bandSlots.values()) slot.prim.setBands(slot.bands, s);
  }

  paneCount(): number {
    return Math.max(0, this.chart.panes().length - 1);
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    if (this.heightsRaf !== 0) cancelAnimationFrame(this.heightsRaf);
    for (const h of this.crosshairHandlers) this.chart.unsubscribeCrosshairMove(h);
    for (const h of this.rangeHandlers) this.chart.timeScale().unsubscribeVisibleLogicalRangeChange(h);
    for (const slot of this.bandSlots.values()) this.release(slot);
    this.bandSlots.clear();
    this.markersApi.detach();
    this.chart.remove();
  }

  // --- internals ---

  private createEntry(spec: SeriesSpec, paneIndex: number): Entry {
    const s = this.scheme;
    switch (kindOf(spec.style)) {
      case "Line":
        return { spec, kind: "Line", api: this.chart.addSeries(LineSeries, lineOptions(spec, s), paneIndex) };
      case "Area":
        return { spec, kind: "Area", api: this.chart.addSeries(AreaSeries, areaOptions(spec, s), paneIndex) };
      case "Histogram":
        return { spec, kind: "Histogram", api: this.chart.addSeries(HistogramSeries, histogramOptions(spec, s), paneIndex) };
    }
  }

  private applyStyle(e: Entry): void {
    const s = this.scheme;
    switch (e.kind) {
      case "Line":
        return e.api.applyOptions(lineOptions(e.spec, s));
      case "Area":
        return e.api.applyOptions(areaOptions(e.spec, s));
      case "Histogram":
        return e.api.applyOptions(histogramOptions(e.spec, s));
    }
  }

  /** The pane id a series renders in: its own when that pane is known, else the price pane. */
  private groupKey(spec: SeriesSpec): string {
    return spec.pane !== PRICE_PANE && this.paneSpecs.has(spec.pane) ? spec.pane : PRICE_PANE;
  }

  /** The chart pane currently showing pane id `key`, if any. */
  private paneOf(key: string, exclude?: Entry): IPaneApi<Time> | null {
    if (key === PRICE_PANE) return this.chart.panes()[0] ?? null;
    for (const e of this.series.values()) if (e !== exclude && this.groupKey(e.spec) === key) return e.api.getPane();
    return null;
  }

  /** Where a series of pane id `key` goes: the pane already showing `key`, else a new pane at the end. */
  private placementIndex(key: string, exclude?: Entry): number {
    return this.paneOf(key, exclude)?.paneIndex() ?? this.chart.panes().length;
  }

  private layout(): void {
    const hosted = new Set<string>();
    for (const e of this.series.values()) {
      const k = this.groupKey(e.spec);
      if (k !== PRICE_PANE) hosted.add(k);
    }
    const map = paneIndexMap([...this.paneSpecs.values()], hosted);
    if (!this.isRegular(map)) this.rebuild(map);
    // Put the panes in map order: ascending targets, so earlier targets stay settled.
    for (const [key, target] of map) {
      if (target === 0) continue;
      const cur = this.paneOf(key)?.paneIndex();
      if (cur !== undefined && cur !== target) this.chart.swapPanes(cur, target);
    }
    this.indexMap = map;
    const panes = this.chart.panes();
    const key = JSON.stringify([...map].map(([id, i]) => [id, i, this.paneSpecs.get(id)?.height_px ?? 0]));
    if (key !== this.sizedKey || panes.length !== this.sizedPanes.length || panes.some((p, i) => p !== this.sizedPanes[i])) {
      this.sizedKey = key;
      this.sizedPanes = panes;
      this.applyHeights();
    }
    this.syncPrimitives();
    this.syncLevels(false);
  }

  /** Every pane id owns exactly one chart pane and nothing else is in it (pane 0: candles and `price`). */
  private isRegular(map: Map<string, number>): boolean {
    const panes = this.chart.panes();
    if (panes.length !== map.size) return false;
    const owner = new Map<IPaneApi<Time>, string>([[panes[0]!, PRICE_PANE]]);
    for (const e of this.series.values()) {
      const k = this.groupKey(e.spec);
      const o = owner.get(e.api.getPane());
      if (o === undefined) {
        if (k === PRICE_PANE) return false;
        owner.set(e.api.getPane(), k);
      } else if (o !== k) return false;
    }
    return owner.size === map.size;
  }

  /**
   * Re-create every overlay series in ascending pane order (recovers from any irregular layout).
   * All are removed first, leaving only pane 0, so the first series of target `t` creates pane `t`.
   */
  private rebuild(map: Map<string, number>): void {
    const target = (e: Entry): number => map.get(this.groupKey(e.spec)) ?? 0;
    const entries = [...this.series.values()].sort((a, b) => target(a) - target(b));
    // Overlay series only ever receive `{ time, value }` points.
    const saved = entries.map((e) => [...e.api.data()] as LineData<Time>[]);
    for (const e of entries) this.chart.removeSeries(e.api);
    entries.forEach((e, i) => {
      const fresh = this.createEntry(e.spec, target(e));
      fresh.api.setData(saved[i]!);
      this.series.set(e.spec.id, fresh);
    });
  }

  /**
   * Sub-panes get their `height_px`, the price pane the rest. Sizes are set as stretch factors equal
   * to pixel heights over the current total, which is exact for any number of panes. Deferred to the
   * next frame while the chart has not been laid out (total height 0).
   */
  private applyHeights(): void {
    const panes = this.chart.panes();
    if (panes.length < 2) return;
    // Not the sum of pane heights: those lag a pane LWC just removed or created until it lays out again.
    const total = this.chart.chartElement().clientHeight - this.chart.timeScale().height();
    if (total <= 0) {
      if (this.heightsRaf === 0 && typeof requestAnimationFrame === "function") {
        this.heightsRaf = requestAnimationFrame(() => {
          this.heightsRaf = 0;
          if (!this.destroyed) this.applyHeights();
        });
      }
      return;
    }
    let sub = 0;
    for (const [id, i] of this.indexMap) {
      const pane = panes[i];
      if (i === 0 || pane === undefined) continue;
      const h = Math.max(MIN_SUB_PANE_PX, this.paneSpecs.get(id)?.height_px ?? MIN_SUB_PANE_PX);
      pane.setStretchFactor(h);
      sub += h;
    }
    panes[0]!.setStretchFactor(Math.max(MIN_PRICE_PANE_PX, total - sub));
  }

  /** Attach each pane id's band primitive to the chart pane now showing that id (or detach it). */
  private syncPrimitives(): void {
    for (const [key, slot] of this.bandSlots) {
      const target = this.indexMap.has(key) ? this.paneOf(key) : null;
      if (slot.host === target) continue;
      this.release(slot);
      if (target !== null) {
        target.attachPrimitive(slot.prim);
        slot.host = target;
      }
    }
  }

  private release(slot: BandSlot): void {
    if (slot.host === null) return;
    if (this.chart.panes().includes(slot.host)) slot.host.detachPrimitive(slot.prim);
    else slot.prim.detached(); // its pane is gone: just drop the chart reference
    slot.host = null;
  }

  /** Price lines live on the first series of their pane; recreate them all when a host changes. */
  private syncLevels(force: boolean): void {
    const hosts = this.levels.map((l) => this.levelHost(l));
    const same = hosts.length === this.lines.length && hosts.every((h, i) => h === (this.lines[i]?.host ?? null));
    if (same && !force) return;
    const alive = new Set<ISeriesApi<SeriesType>>([this.candles]);
    for (const e of this.series.values()) alive.add(e.api);
    for (const l of this.lines) if (l !== null && alive.has(l.host)) l.host.removePriceLine(l.line);
    this.lines = this.levels.map((spec, i) => {
      const host = hosts[i] ?? null;
      return host === null ? null : { host, line: host.createPriceLine(priceLineOptions(spec, this.scheme)) };
    });
  }

  private levelHost(l: LevelSpec): ISeriesApi<SeriesType> | null {
    if (l.pane === PRICE_PANE) return this.candles;
    if (!this.indexMap.has(l.pane)) return null;
    return this.paneOf(l.pane)?.getSeries()[0] ?? null;
  }

  private applyMarkers(): void {
    const s = this.scheme;
    const sorted = [...this.markers].sort((a, b) => a.timeMs - b.timeMs);
    const out: SeriesMarker<Time>[] = sorted.map((m) => ({
      time: sec(m.timeMs),
      position: MARKER_POSITION[m.position],
      shape: m.shape,
      color: resolveColor(m.color, s),
      text: m.text,
      id: m.id,
    }));
    this.markersApi.setMarkers(out);
  }

  /** x of a bucket start; ±Infinity before/after the candle grid; null when it has no coordinate. */
  private xOf(tMs: number): number | null {
    const x = this.chart.timeScale().timeToCoordinate(sec(tMs));
    if (x !== null) return x;
    if (this.gridFirst === null || this.gridLast === null) return null;
    if (tMs < this.gridFirst) return -Infinity;
    if (tMs > this.gridLast) return Infinity;
    return null;
  }
}

export function createLwcSurface(container: HTMLElement, o: LwcSurfaceOptions): LwcSurface {
  return new LwcChartSurface(container, o);
}
