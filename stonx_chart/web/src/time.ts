/** Chart aggregation intervals, in seconds. Exactly these four are supported. */
export type IntervalSec = 1 | 5 | 60 | 300;

export const INTERVALS: readonly IntervalSec[] = [1, 5, 60, 300];

/** Start (epoch ms) of the `interval`-second bucket containing `tMs`. */
export function bucketOf(tMs: number, interval: IntervalSec): number {
  const step = interval * 1000;
  return Math.floor(tMs / step) * step;
}
