/**
 * Pure pane placement: which chart pane index each overlay pane id renders in. Pane 0 is always the
 * price pane; the hosted sub-panes follow in ascending `order` (ties by id), so indices are 1..n with no gaps.
 */
import type { PaneSpec } from "../protocol/types";

export const PRICE_PANE = "price";

/** `price` → 0, then the panes in `hosted` sorted by `order` → 1..n. Unhosted panes are absent. */
export function paneIndexMap(panes: PaneSpec[], hosted: Set<string>): Map<string, number> {
  const out = new Map<string, number>([[PRICE_PANE, 0]]);
  const sorted = panes
    .filter((p) => p.id !== PRICE_PANE && hosted.has(p.id))
    .sort((a, b) => a.order - b.order || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  for (const p of sorted) if (!out.has(p.id)) out.set(p.id, out.size);
  return out;
}
