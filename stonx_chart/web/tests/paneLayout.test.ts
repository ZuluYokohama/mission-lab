import { expect, test } from "vitest";
import type { PaneSpec } from "../src/protocol/types";
import { paneIndexMap } from "../src/surface/paneLayout";

const pane = (id: string, order: number): PaneSpec => ({
  id,
  kind: "pane",
  group: "g",
  pane: id,
  title: id,
  height_px: 120,
  order,
});

test("paneIndexMap orders hosted panes", () =>
  expect([...paneIndexMap([pane("b", 2), pane("a", 1), pane("c", 3)], new Set(["b", "c"]))]).toEqual([
    ["price", 0],
    ["b", 1],
    ["c", 2],
  ]));
