/**
 * Semantic color tokens of the wire protocol, resolved per color scheme. Pure: no chart library here.
 * Overlays carry tokens (`regime.fragmented`, ...), never raw colors; unknown tokens render as `neutral`.
 */

export type Scheme = "light" | "dark";

type Rgb = readonly [number, number, number];

const PALETTE: Record<Scheme, Record<string, Rgb>> = {
  light: {
    "regime.fragmented": [217, 119, 6],
    "regime.high_curl": [124, 58, 237],
    "regime.holonomy": [13, 148, 136],
    "state.halt": [220, 38, 38],
    "event.reset": [37, 99, 235],
    "event.curl_surge": [219, 39, 119],
    "series.primary": [29, 78, 216],
    "series.secondary": [234, 88, 12],
    "series.quote": [8, 145, 178],
    "level.threshold": [161, 98, 7],
    neutral: [100, 116, 139],
  },
  dark: {
    "regime.fragmented": [251, 191, 36],
    "regime.high_curl": [167, 139, 250],
    "regime.holonomy": [45, 212, 191],
    "state.halt": [248, 113, 113],
    "event.reset": [96, 165, 250],
    "event.curl_surge": [244, 114, 182],
    "series.primary": [147, 197, 253],
    "series.secondary": [251, 146, 60],
    "series.quote": [34, 211, 238],
    "level.threshold": [250, 204, 21],
    neutral: [148, 163, 184],
  },
};

/** The 11 global tokens. */
export const TOKENS: readonly string[] = Object.keys(PALETTE.light);

const rgba = ([r, g, b]: Rgb, alpha: number): string => `rgba(${r}, ${g}, ${b}, ${alpha})`;

/** `rgba(r, g, b, a)` for `token` in `scheme`; unknown tokens resolve as `neutral`. */
export function resolveColor(token: string, scheme: Scheme, alpha = 1): string {
  const palette = PALETTE[scheme];
  const rgb = Object.hasOwn(palette, token) ? palette[token]! : palette.neutral!;
  return rgba(rgb, alpha);
}

export interface ChartColors {
  background: string;
  text: string;
  grid: string;
  up: string;
  down: string;
}

const CHART: Record<Scheme, Record<keyof ChartColors, Rgb>> = {
  light: {
    background: [255, 255, 255],
    text: [31, 41, 55],
    grid: [229, 231, 235],
    up: [22, 163, 74],
    down: [220, 38, 38],
  },
  dark: {
    background: [15, 23, 42],
    text: [226, 232, 240],
    grid: [30, 41, 59],
    up: [34, 197, 94],
    down: [239, 68, 68],
  },
};

export function chartColors(scheme: Scheme): ChartColors {
  const c = CHART[scheme];
  return {
    background: rgba(c.background, 1),
    text: rgba(c.text, 1),
    grid: rgba(c.grid, 1),
    up: rgba(c.up, 1),
    down: rgba(c.down, 1),
  };
}
