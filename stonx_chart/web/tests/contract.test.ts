/**
 * Cross-language contract via the golden fixtures: the 300 s snapshot followed by the 300→600 s stream
 * must leave the surface exactly as the 600 s snapshot does, at every interval.
 */
import { expect, test } from "vitest";
import { ChartController } from "../src/app/controller";
import { decodeMessage } from "../src/protocol/decode";
import { FakeSurface } from "../src/surface/fakeSurface";
import type { IntervalSec } from "../src/time";
import { loadFixtureMessages } from "./fixtures";

const FLUSH_EVERY = 20;

/** Decode and handle every message of `files`, flushing after every 20 messages and at the end. */
function run(files: string[], interval: IntervalSec) {
  const fake = new FakeSurface();
  const controller = new ChartController(fake, { interval });
  controller.connectionOpened();
  let n = 0;
  for (const file of files) {
    for (const raw of loadFixtureMessages(file)) {
      const d = decodeMessage(raw);
      if (d.ok) {
        controller.handle(d.message);
        for (let i = 0; i < d.ignoredOverlays; i++) controller.noteDecodeIssue("ignored");
      } else {
        controller.noteDecodeIssue(d.reason === "ignored" ? "ignored" : "malformed");
      }
      if (++n % FLUSH_EVERY === 0) controller.flush();
    }
  }
  controller.flush();
  return { fake, controller };
}

test.each([1, 5, 60, 300] as const)("incremental stream equals snapshot at 600 s (interval %i)", (I) => {
  const a = run(["nvda_snapshot_600s.json"], I);
  const b = run(["nvda_snapshot_300s.json", "nvda_stream_300_600s.json"], I);
  // Non-vacuous: the 600 s state has every kind of render output.
  expect(a.fake.candles.length).toBeGreaterThan(0);
  expect(a.fake.series.size).toBe(5);
  expect(a.fake.bands.length).toBeGreaterThan(0);
  expect(a.fake.markers.length).toBeGreaterThan(0);
  expect(a.fake.levels.length).toBeGreaterThan(0);
  expect(b.fake.candles).toEqual(a.fake.candles);
  expect([...b.fake.series]).toEqual([...a.fake.series]);
  expect(b.fake.bands).toEqual(a.fake.bands);
  expect(b.fake.markers).toEqual(a.fake.markers);
  expect(b.fake.levels).toEqual(a.fake.levels);
  expect(b.fake.panes).toEqual(a.fake.panes);
  expect(b.controller.counters).toEqual({ malformed: 0, ignored: 0, gaps: 0, stale: 0 });
  expect(a.controller.counters).toEqual({ malformed: 0, ignored: 0, gaps: 0, stale: 0 });
  expect(b.controller.state().barCount).toBe(a.controller.state().barCount);
  // The stream half is incremental: exactly one full render (the first flush) in the streamed run.
  expect(b.fake.calls.filter((c) => c.method === "setCandles")).toHaveLength(1);
});
