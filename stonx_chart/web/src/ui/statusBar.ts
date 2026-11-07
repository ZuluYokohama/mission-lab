/** Bottom status bar: connection state, data age, source and dropped-message count. DOM only. */
import type { ControllerState } from "../app/controller";
import type { ConnState } from "../transport/streamClient";
import { formatAge } from "./format";

export interface StatusBarHandlers {
  now: () => number;
}

function cell(root: HTMLElement, className: string): HTMLSpanElement {
  const el = document.createElement("span");
  el.className = className;
  root.append(el);
  return el;
}

function setText(el: HTMLElement, text: string): void {
  if (el.textContent !== text) el.textContent = text;
}

export function createStatusBar(root: HTMLElement, h: StatusBarHandlers): { render(state: ControllerState, conn: ConnState): void } {
  const connEl = cell(root, "status-conn");
  connEl.dataset.testid = "conn-state";
  const ageEl = cell(root, "status-age");
  const sourceEl = cell(root, "status-source");
  const dropEl = cell(root, "status-drops");

  return {
    render(state, conn) {
      setText(connEl, conn);
      connEl.dataset.state = conn;
      const age = state.lastMessageAtMs === null ? null : h.now() - state.lastMessageAtMs;
      setText(ageEl, `data age ${formatAge(age)}`);
      setText(sourceEl, state.source ?? "");
      setText(dropEl, state.dropCount > 0 ? `${state.dropCount} dropped` : "");
    },
  };
}
