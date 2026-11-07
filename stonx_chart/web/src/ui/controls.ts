/** Toolbar: asset picker, interval picker and the jump-to-live button. DOM only. */
import type { ControllerState } from "../app/controller";
import type { ConnState } from "../transport/streamClient";
import { INTERVALS, type IntervalSec } from "../time";

export interface ControlsHandlers {
  /** The asset to show until the first snapshot arrives. */
  initialAsset: string;
  onAsset: (asset: string) => void;
  onInterval: (interval: IntervalSec) => void;
  onJumpLive: () => void;
  isAtLiveEdge: () => boolean;
}

const INTERVAL_LABEL: Record<IntervalSec, string> = { 1: "1s", 5: "5s", 60: "1m", 300: "5m" };

function labelled(text: string, select: HTMLSelectElement): HTMLLabelElement {
  const label = document.createElement("label");
  label.className = "control";
  const span = document.createElement("span");
  span.textContent = text;
  label.append(span, select);
  return label;
}

export function createControls(root: HTMLElement, h: ControlsHandlers): { render(state: ControllerState, conn: ConnState): void } {
  const assetSelect = document.createElement("select");
  assetSelect.dataset.testid = "asset-select";
  assetSelect.setAttribute("aria-label", "Asset");

  const intervalSelect = document.createElement("select");
  intervalSelect.dataset.testid = "interval-select";
  intervalSelect.setAttribute("aria-label", "Interval");
  for (const i of INTERVALS) {
    const o = document.createElement("option");
    o.value = String(i);
    o.textContent = INTERVAL_LABEL[i];
    intervalSelect.append(o);
  }

  const jump = document.createElement("button");
  jump.type = "button";
  jump.dataset.testid = "jump-live";
  jump.className = "jump-live";
  jump.textContent = "Jump to live";
  jump.hidden = true;

  root.append(labelled("Asset", assetSelect), labelled("Interval", intervalSelect), jump);

  // The picked asset, kept until a snapshot says otherwise, so the old asset's state never snaps it back.
  let selected = h.initialAsset;
  let snapshotAsset: string | null = null;
  let optionsKey = "";

  assetSelect.addEventListener("change", () => {
    selected = assetSelect.value;
    h.onAsset(selected);
  });
  intervalSelect.addEventListener("change", () => {
    h.onInterval(Number(intervalSelect.value) as IntervalSec);
  });
  jump.addEventListener("click", () => h.onJumpLive());

  return {
    render(state) {
      if (state.asset !== snapshotAsset) {
        snapshotAsset = state.asset;
        if (state.asset !== null) selected = state.asset;
      }
      const list = state.assets.length > 0 ? [...state.assets] : [];
      if (!list.includes(selected)) list.unshift(selected);
      const key = list.join("\u0000");
      if (key !== optionsKey) {
        optionsKey = key;
        assetSelect.replaceChildren(
          ...list.map((symbol) => {
            const o = document.createElement("option");
            o.value = symbol;
            o.textContent = symbol;
            return o;
          }),
        );
      }
      if (assetSelect.value !== selected) assetSelect.value = selected;
      const interval = String(state.interval);
      if (intervalSelect.value !== interval) intervalSelect.value = interval;
      jump.hidden = h.isAtLiveEdge();
    },
  };
}
