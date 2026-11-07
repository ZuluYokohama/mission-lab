/**
 * Time bands drawn behind a pane's series as a Lightweight Charts pane primitive (`zOrder: 'bottom'`).
 * One `BandsPrimitive` serves one chart pane; geometry uses public APIs only (`timeToCoordinate`
 * through the `xOf` callback, `timeScale().options().barSpacing`).
 */
import type { CanvasRenderingTarget2D } from "fancy-canvas";
import type {
  IChartApiBase,
  IPanePrimitive,
  IPanePrimitivePaneView,
  IPrimitivePaneRenderer,
  PaneAttachedParameter,
  PrimitivePaneViewZOrder,
  Time,
} from "lightweight-charts";
import type { BandRender } from "../overlay/bucket";
import { resolveColor, type Scheme } from "./theme";

const FILL_ALPHA = 0.18;
const LABEL_FONT_PX = 11;
const LABEL_PAD_PX = 4;

export interface BandSpan {
  band: BandRender;
  /** Media (CSS pixel) x range within the pane, clipped to [0, width]. */
  x0: number;
  x1: number;
}

/**
 * Horizontal extent of each band that intersects the pane. `xOf` maps a bucket start to its x, or
 * ±Infinity for a time before/after the data, or null when it has no coordinate. A band covers
 * half a bar beyond its first and last bucket; an open band (`endMs` null) runs to the right edge.
 * Bands whose both edges have no coordinate, or that lie wholly off-screen on one side, are skipped.
 */
export function bandSpans(
  bands: readonly BandRender[],
  xOf: (tMs: number) => number | null,
  barSpacing: number,
  width: number,
): BandSpan[] {
  const half = barSpacing / 2;
  const out: BandSpan[] = [];
  for (const band of bands) {
    const a = xOf(band.startMs);
    const e = band.endMs === null ? null : xOf(band.endMs);
    if (a === null && (band.endMs === null || e === null)) continue;
    const left = a === null ? -Infinity : a - half;
    const right = band.endMs === null ? width : e === null ? Infinity : e + half;
    if (right <= 0 || left >= width) continue;
    const x0 = Math.max(0, left);
    const x1 = Math.min(width, right);
    if (x1 > x0) out.push({ band, x0, x1 });
  }
  return out;
}

class BandsRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly owner: BandsPrimitive) {}

  draw(target: CanvasRenderingTarget2D): void {
    const spans = this.owner.spansFor(target);
    if (spans.length === 0) return;
    const scheme = this.owner.scheme();
    target.useBitmapCoordinateSpace(({ context, bitmapSize, horizontalPixelRatio: hpr, verticalPixelRatio: vpr }) => {
      for (const s of spans) {
        const x0 = Math.round(s.x0 * hpr);
        const x1 = Math.round(s.x1 * hpr);
        if (x1 <= x0) continue;
        context.fillStyle = resolveColor(s.band.color, scheme, FILL_ALPHA);
        context.fillRect(x0, 0, x1 - x0, bitmapSize.height);
      }
      context.font = `${Math.round(LABEL_FONT_PX * vpr)}px sans-serif`;
      context.textBaseline = "top";
      const pad = Math.round(LABEL_PAD_PX * hpr);
      const padY = Math.round(LABEL_PAD_PX * vpr);
      const lineH = Math.round(LABEL_FONT_PX * vpr);
      for (const s of spans) {
        if (s.band.label === "") continue;
        const x0 = Math.round(s.x0 * hpr);
        const avail = Math.round(s.x1 * hpr) - x0;
        if (context.measureText(s.band.label).width + 2 * pad > avail || lineH + 2 * padY > bitmapSize.height) continue;
        context.fillStyle = resolveColor(s.band.color, scheme);
        context.fillText(s.band.label, x0 + pad, padY);
      }
    });
  }
}

class BandsPaneView implements IPanePrimitivePaneView {
  private readonly r: BandsRenderer;
  constructor(owner: BandsPrimitive) {
    this.r = new BandsRenderer(owner);
  }
  zOrder(): PrimitivePaneViewZOrder {
    return "bottom";
  }
  renderer(): IPrimitivePaneRenderer {
    return this.r;
  }
}

export class BandsPrimitive implements IPanePrimitive<Time> {
  private chart: IChartApiBase<Time> | null = null;
  private requestUpdate: (() => void) | null = null;
  private bands: BandRender[] = [];
  private currentScheme: Scheme = "light";
  private readonly views: readonly IPanePrimitivePaneView[];

  /** `xOf` as in `bandSpans`, supplied by the surface (it knows the candle grid's extent). */
  constructor(private readonly xOf: (tMs: number) => number | null) {
    this.views = [new BandsPaneView(this)];
  }

  setBands(bands: BandRender[], scheme: Scheme): void {
    this.bands = bands.map((b) => ({ ...b }));
    this.currentScheme = scheme;
    this.requestUpdate?.();
  }

  attached(param: PaneAttachedParameter<Time>): void {
    this.chart = param.chart;
    this.requestUpdate = param.requestUpdate;
    this.requestUpdate();
  }

  detached(): void {
    this.chart = null;
    this.requestUpdate = null;
  }

  paneViews(): readonly IPanePrimitivePaneView[] {
    return this.views;
  }

  /** @internal Spans for the pane being drawn, in media pixels. */
  spansFor(target: CanvasRenderingTarget2D): BandSpan[] {
    const chart = this.chart;
    if (chart === null || this.bands.length === 0) return [];
    const width = target.useMediaCoordinateSpace(({ mediaSize }) => mediaSize.width);
    return bandSpans(this.bands, this.xOf, chart.timeScale().options().barSpacing, width);
  }

  /** @internal */
  scheme(): Scheme {
    return this.currentScheme;
  }
}
