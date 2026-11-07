/** Crosshair readout: the bar's OHLC and volume plus each visible series' value at that time. DOM only. */
import type { ControllerState, Readout } from "../app/controller";
import type { ConnState } from "../transport/streamClient";
import { formatNumber, formatTime } from "./format";

export interface ReadoutHandlers {
  readoutAt: (tMs: number) => Readout | null;
}

export function createReadout(
  root: HTMLElement,
  h: ReadoutHandlers,
): { setCrosshair(tMs: number | null): void; render(state: ControllerState, conn: ConnState): void } {
  root.dataset.testid = "readout";
  root.hidden = true;
  let at: number | null = null;
  let shown = "";

  const draw = (): void => {
    const r = at === null ? null : h.readoutAt(at);
    if (at === null || r === null) {
      root.hidden = true;
      shown = "";
      return;
    }
    const parts: string[] = [formatTime(at, true)];
    if (r.bar !== null) {
      const b = r.bar;
      parts.push(
        `O ${formatNumber(b.o, 2)}`,
        `H ${formatNumber(b.h, 2)}`,
        `L ${formatNumber(b.l, 2)}`,
        `C ${formatNumber(b.c, 2)}`,
        `Vol ${formatNumber(b.vol, 0)}`,
      );
    }
    for (const v of r.values) parts.push(`${v.id} ${formatNumber(v.value, 3)}`);
    const text = parts.join("  ");
    if (text !== shown) {
      shown = text;
      root.textContent = text;
    }
    root.hidden = false;
  };

  return {
    setCrosshair(tMs) {
      at = tMs;
      draw();
    },
    // Live data can change under a stationary crosshair.
    render() {
      if (at !== null) draw();
    },
  };
}
