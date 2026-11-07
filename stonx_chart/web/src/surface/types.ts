/**
 * The rendering interface the controller drives. `LwcSurface` implements it with Lightweight Charts
 * (the only module that imports the library); `FakeSurface` records calls for tests. All times are
 * epoch milliseconds on bucket boundaries of the active interval.
 */
import type { CandleItem } from "../bars/barStore";
import type { BandRender, MarkerRender, Point } from "../overlay/bucket";
import type { LevelSpec, PaneSpec, SeriesSpec } from "../protocol/types";

export interface VisibleRange {
  fromMs: number;
  toMs: number;
}

export interface ChartSurface {
  /** Replace the whole dense candle grid. */
  setCandles(items: CandleItem[]): void;
  /** Update the last candle or append one; `item.t` is never older than the last candle. */
  updateCandle(item: CandleItem): void;

  /** The visible panes (other than price), each hosting at least one visible series. */
  setPanes(panes: PaneSpec[]): void;
  /** Create or replace a series with its full bucketed data. */
  setSeries(spec: SeriesSpec, data: Point[]): void;
  /** Update the series' last point or append one; `p.t` is never older than its last point. */
  updateSeriesPoint(id: string, p: Point): void;
  removeSeries(id: string): void;

  setLevels(levels: LevelSpec[]): void;
  setBands(bands: BandRender[]): void;
  setMarkers(markers: MarkerRender[]): void;

  getVisibleRange(): VisibleRange | null;
  setVisibleRange(r: VisibleRange): void;
  isAtLiveEdge(): boolean;
  scrollToLive(): void;

  setDimmed(d: boolean): void;
  onCrosshair(cb: (tMs: number | null) => void): void;
  onViewChange(cb: () => void): void;
  destroy(): void;
}
