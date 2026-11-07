import { describe, expect, test } from "vitest";
import { decodeMessage } from "../src/protocol/decode";
import { loadFixtureMessages } from "./fixtures";

const env = { v: 1, source: "SIMULATED_DEMO", seq: 1 };

const validBand = { id: "band.a", kind: "band", group: "regime", pane: "price", start_ms: 1000, end_ms: null, color: "regime.x", label: "X" };
const validMarker = { id: "marker.a", kind: "marker", group: "events", pane: "price", time_ms: 1000, position: "below", shape: "circle", color: "event.x", text: "t" };
const validSeries = { id: "series.a", kind: "series", group: "g", pane: "price", style: "line", color: "series.primary", width: 1, agg: "last", precision: 2 };

const msg = (extra: Record<string, unknown>): string => JSON.stringify({ ...env, type: "heartbeat", now_ms: 1000, ...extra });
const hb = (extra: Record<string, unknown> = {}): string => msg(extra);
const upsert = (overlay: Record<string, unknown>): string => msg({ type: "overlay.upsert", overlay });
const points = (extra: Record<string, unknown> = {}): string =>
  msg({ type: "series.points", id: "series.a", points: { t: [1000], v: [1.5] }, ...extra });
const status = (extra: Record<string, unknown>): string => msg({ type: "status", ...extra });

const goodBars = { t: [1000], o: [1], h: [2], l: [0.5], c: [1.5], vol: [10] };
const snapshot = (extra: Record<string, unknown>): string =>
  msg({
    type: "snapshot",
    asset: "NVDA",
    assets: [{ id: 0, symbol: "NVDA" }],
    session_start_ms: 1000,
    bars: goodBars,
    overlays: [],
    points: {},
    ...extra,
  });
const snapshotWith = (overlays: unknown[]): string => snapshot({ overlays });
const snapshotWithBars = (bars: Record<string, unknown>): string => snapshot({ bars });

describe("decodeMessage", () => {
  test("decodes every fixture message", () => {
    for (const n of ["nvda_snapshot_300s.json", "nvda_stream_300_600s.json", "nvda_snapshot_600s.json"])
      for (const raw of loadFixtureMessages(n)) expect(decodeMessage(raw)).toMatchObject({ ok: true, ignoredOverlays: 0 });
  });

  test("malformed inputs", () => {
    for (const raw of [
      "nope",
      "[]",
      '{"v":1,"type":"heartbeat","source":"x"}' /* no seq */,
      '{"v":[1],"type":"series.points","source":"x","seq":1,"id":"a","t":[1]}' /* flat points */,
    ])
      expect(decodeMessage(raw)).toMatchObject({ ok: false, reason: "malformed" });
  });

  test("wrong version is incompatible", () => expect(decodeMessage(hb({ v: 2 }))).toMatchObject({ ok: false, reason: "incompatible" }));

  test("unknown type and kind are ignored", () => {
    expect(decodeMessage(msg({ type: "wat" }))).toMatchObject({ reason: "ignored" });
    expect(decodeMessage(upsert({ kind: "cloud" }))).toMatchObject({ reason: "ignored" });
  });

  test("snapshot drops unknown overlay kinds", () => {
    const r = decodeMessage(snapshotWith([{ ...validBand, kind: "cloud" }, validBand]));
    expect(r).toMatchObject({ ok: true, ignoredOverlays: 1 });
    expect((r as any).message.overlays).toHaveLength(1);
  });

  test("enum and shape violations", () => {
    expect(decodeMessage(upsert({ ...validMarker, position: "left" })).ok).toBe(false);
    expect(decodeMessage(upsert({ ...validSeries, agg: "median" })).ok).toBe(false);
    expect(decodeMessage(points({ points: { t: [1000], v: [] } })).ok).toBe(false);
    expect(decodeMessage(snapshotWithBars({ t: [1000], o: [] })).ok).toBe(false);
  });

  test("status message optional", () => expect(decodeMessage(status({ feed_state: "ok", drop_count: 0 })).ok).toBe(true));
});
