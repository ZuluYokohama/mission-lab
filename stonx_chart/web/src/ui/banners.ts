/** Notice banners. The source banner has no close control and shows whenever the data is simulated. */
import type { ControllerState } from "../app/controller";
import type { ConnState } from "../transport/streamClient";

export const SOURCE_BANNER_TEXT = "SIMULATED DEMO DATA — not market data";
export const SIMULATED_SOURCE = "SIMULATED_DEMO";

function banner(root: HTMLElement, testid: string, className: string): HTMLDivElement {
  const el = document.createElement("div");
  el.dataset.testid = testid;
  el.className = `banner ${className}`;
  el.setAttribute("role", "status");
  el.hidden = true;
  root.append(el);
  return el;
}

function show(el: HTMLElement, visible: boolean, text?: string): void {
  if (el.hidden === visible) el.hidden = !visible;
  if (visible && text !== undefined && el.textContent !== text) el.textContent = text;
}

export function createBanners(root: HTMLElement): { render(state: ControllerState, conn: ConnState): void } {
  const source = banner(root, "source-banner", "banner-source");
  source.textContent = SOURCE_BANNER_TEXT;
  const stale = banner(root, "stale-banner", "banner-warn");
  const incompatible = banner(root, "incompatible-banner", "banner-error");
  const degraded = banner(root, "degraded-banner", "banner-warn");

  return {
    render(state, conn) {
      show(source, state.source === SIMULATED_SOURCE);
      show(stale, conn === "stale" || conn === "reconnecting", "No recent data from the feed. Reconnecting…");
      show(incompatible, conn === "incompatible", "This page cannot read the feed's protocol version. Update the page to continue.");
      show(
        degraded,
        state.feedState === "degraded",
        state.statusMessage !== null && state.statusMessage !== "" ? `Feed degraded: ${state.statusMessage}` : "Feed degraded",
      );
    },
  };
}
