/** Persisted viewer preferences. Every storage access is guarded; the page works without storage. */
import { INTERVALS, type IntervalSec } from "../time";

export interface Prefs {
  asset: string;
  interval: IntervalSec;
  hiddenGroups: string[];
}

export const PREFS_KEY = "stonx_chart.prefs.v1";
export const DEFAULT_PREFS: Prefs = { asset: "NVDA", interval: 60, hiddenGroups: [] };

/** `localStorage`, or null when it is missing or blocked. */
export function safeStorage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

const isIntervalSec = (v: unknown): v is IntervalSec => (INTERVALS as readonly unknown[]).includes(v);

/** Each invalid or missing field falls back to its default. */
export function loadPrefs(s: Pick<Storage, "getItem"> | null): Prefs {
  const prefs: Prefs = { ...DEFAULT_PREFS, hiddenGroups: [] };
  if (s === null) return prefs;
  try {
    const raw = s.getItem(PREFS_KEY);
    if (raw === null) return prefs;
    const o: unknown = JSON.parse(raw);
    if (typeof o !== "object" || o === null) return prefs;
    const r = o as Record<string, unknown>;
    if (typeof r.asset === "string" && r.asset !== "") prefs.asset = r.asset;
    if (isIntervalSec(r.interval)) prefs.interval = r.interval;
    if (Array.isArray(r.hiddenGroups) && r.hiddenGroups.every((g) => typeof g === "string")) {
      prefs.hiddenGroups = [...(r.hiddenGroups as string[])];
    }
  } catch {
    // Unreadable storage or bad JSON: use what was parsed so far (defaults).
  }
  return prefs;
}

export function savePrefs(s: Pick<Storage, "setItem"> | null, p: Prefs): void {
  if (s === null) return;
  try {
    s.setItem(PREFS_KEY, JSON.stringify(p));
  } catch {
    // Quota or blocked storage: preferences simply do not persist.
  }
}
