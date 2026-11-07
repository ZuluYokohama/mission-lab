/** Display formatting. Times are shown in New York time; the timestamps themselves are never shifted. */
import type { IntervalSec } from "../time";

export const DISPLAY_TZ = "America/New_York";

const make = (seconds: boolean): Intl.DateTimeFormat =>
  new Intl.DateTimeFormat("en-GB", {
    timeZone: DISPLAY_TZ,
    hour: "2-digit",
    minute: "2-digit",
    ...(seconds ? { second: "2-digit" as const } : {}),
    hourCycle: "h23",
  });

const WITH_SECONDS = make(true);
const WITHOUT_SECONDS = make(false);

/** 24-hour `HH:mm[:ss]` in New York time. */
export function formatTime(tMs: number, withSeconds: boolean): string {
  return (withSeconds ? WITH_SECONDS : WITHOUT_SECONDS).format(tMs);
}

/** Axis tick label: seconds are shown only when the interval is below one minute. */
export function formatTick(tMs: number, interval: IntervalSec): string {
  return formatTime(tMs, interval < 60);
}

/** `—` when unknown, else `0.4 s`, `12 s` or `3 min`. */
export function formatAge(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return "—";
  const age = Math.max(0, ms);
  if (age < 1000) return `${(age / 1000).toFixed(1)} s`;
  if (age < 60_000) return `${Math.round(age / 1000)} s`;
  return `${Math.round(age / 60_000)} min`;
}

/** `precision` comes from a server spec, so it is clamped to what `toFixed` accepts rather than trusted. */
export function formatNumber(v: number, precision: number): string {
  const digits = Math.min(20, Math.max(0, Math.trunc(Number.isFinite(precision) ? precision : 0)));
  return Number.isFinite(v) ? v.toFixed(digits) : "—";
}
