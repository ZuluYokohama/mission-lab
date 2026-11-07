import { expect, test } from "vitest";
import type { BandRender } from "../src/overlay/bucket";
import { bandSpans } from "../src/surface/lwcBands";

const band = (id: string, startMs: number, endMs: number | null): BandRender => ({
  id,
  pane: "price",
  color: "state.halt",
  label: id,
  startMs,
  endMs,
});

// One bar per 1000 ms, 10 px apart, t=0 at x=0; times outside [0, 100_000] are off the time scale.
const xOf = (tMs: number): number | null => (tMs < 0 ? -Infinity : tMs > 100_000 ? Infinity : tMs / 100);
const spans = (bands: BandRender[], width = 500) =>
  bandSpans(bands, xOf, 10, width).map((s) => [s.band.id, s.x0, s.x1]);

test("closed band spans half a bar beyond both edges", () => expect(spans([band("a", 10_000, 20_000)])).toEqual([["a", 95, 205]]));

test("open band extends to the pane's right edge", () => expect(spans([band("a", 10_000, null)])).toEqual([["a", 95, 500]]));

test("bands entirely off-screen on one side are skipped, partly visible ones are clipped", () =>
  expect(
    spans([band("left", -9_000, -1_000), band("right", 60_000, 70_000), band("clip", -5_000, 3_000), band("openRight", 80_000, null)]),
  ).toEqual([["clip", 0, 35]]));

test("a band whose both edges have no coordinate is skipped", () =>
  expect(bandSpans([band("a", 1, 2)], () => null, 10, 500)).toEqual([]));
