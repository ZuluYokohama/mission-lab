/** Overlay group toggles. `aria-pressed="true"` means the group is visible. DOM only. */
import type { ControllerState } from "../app/controller";
import type { ConnState } from "../transport/streamClient";

export interface LegendHandlers {
  onToggle: (group: string, visible: boolean) => void;
}

export function createLegend(root: HTMLElement, h: LegendHandlers): { render(state: ControllerState, conn: ConnState): void } {
  const buttons = new Map<string, HTMLButtonElement>();
  let order = "";

  return {
    render(state) {
      // A hidden group from the saved prefs stays listed (as a toggle) before its overlays arrive.
      const groups = [...state.groups];
      for (const g of state.hiddenGroups) if (!groups.includes(g)) groups.push(g);
      const hidden = new Set(state.hiddenGroups);

      const key = groups.join("\u0000");
      if (key !== order) {
        order = key;
        const next: HTMLButtonElement[] = [];
        for (const g of groups) {
          let b = buttons.get(g);
          if (b === undefined) {
            b = document.createElement("button");
            b.type = "button";
            b.className = "legend-item";
            b.dataset.testid = "legend-item";
            b.dataset.group = g;
            b.textContent = g;
            buttons.set(g, b);
            b.addEventListener("click", () => {
              h.onToggle(g, b!.getAttribute("aria-pressed") !== "true");
            });
          }
          next.push(b);
        }
        for (const [g, b] of buttons) if (!groups.includes(g)) buttons.delete(g);
        root.replaceChildren(...next);
      }
      for (const [g, b] of buttons) {
        const pressed = hidden.has(g) ? "false" : "true";
        if (b.getAttribute("aria-pressed") !== pressed) b.setAttribute("aria-pressed", pressed);
      }
    },
  };
}
