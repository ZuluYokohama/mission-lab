/**
 * Wire types of the StonX chart stream (the TypeScript half of the contract in
 * `stonx_chart/feed/wire.py`). All times are integer milliseconds since the epoch.
 */

export interface BaseOverlay {
  id: string;
  kind: string;
  group: string;
  pane: string;
}

export interface PaneSpec extends BaseOverlay {
  kind: "pane";
  title: string;
  height_px: number;
  order: number;
}

export interface BandSpec extends BaseOverlay {
  kind: "band";
  start_ms: number;
  end_ms: number | null;
  color: string;
  label: string;
}

export interface MarkerSpec extends BaseOverlay {
  kind: "marker";
  time_ms: number;
  position: "above" | "below" | "inBar";
  shape: "arrowUp" | "arrowDown" | "circle" | "square";
  color: string;
  text: string;
}

export type Agg = "last" | "mean" | "min" | "max" | "sum";

export interface SeriesSpec extends BaseOverlay {
  kind: "series";
  style: "line" | "area" | "histogram" | "step";
  color: string;
  width: number;
  agg: Agg;
  precision: number;
}

export interface LevelSpec extends BaseOverlay {
  kind: "level";
  value: number;
  color: string;
  label: string;
  dash: boolean;
}

export type OverlaySpec = PaneSpec | BandSpec | MarkerSpec | SeriesSpec | LevelSpec;

/** Bars as columns: one equal-length array per field. */
export interface BarCols {
  t: number[];
  o: number[];
  h: number[];
  l: number[];
  c: number[];
  vol: number[];
}

/** One series' points as columns: `t` and `v` have equal length. */
export interface PointCols {
  t: number[];
  v: number[];
}

export interface Envelope {
  v: 1;
  source: string;
  seq: number;
}

export interface AssetRef {
  id: number;
  symbol: string;
}

export interface SnapshotMessage extends Envelope {
  type: "snapshot";
  asset: string;
  assets: AssetRef[];
  session_start_ms: number;
  bars: BarCols;
  overlays: OverlaySpec[];
  points: Record<string, PointCols>;
}

export interface BarMessage extends Envelope {
  type: "bar";
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  vol: number;
}

export interface OverlayUpsertMessage extends Envelope {
  type: "overlay.upsert";
  overlay: OverlaySpec;
}

export interface OverlayRemoveMessage extends Envelope {
  type: "overlay.remove";
  id: string;
}

/** Points of one series, nested under `points` (a top-level `v` would clash with the envelope version). */
export interface SeriesPointsMessage extends Envelope {
  type: "series.points";
  id: string;
  points: PointCols;
}

export interface HeartbeatMessage extends Envelope {
  type: "heartbeat";
  now_ms: number;
}

export interface StatusMessage extends Envelope {
  type: "status";
  feed_state: string;
  drop_count: number;
  message?: string;
}

export type StreamMessage =
  | SnapshotMessage
  | BarMessage
  | OverlayUpsertMessage
  | OverlayRemoveMessage
  | SeriesPointsMessage
  | HeartbeatMessage
  | StatusMessage;
