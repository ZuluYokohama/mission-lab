/**
 * The client's single trust boundary: raw SSE `data:` text in, a validated `StreamMessage` out.
 *
 * Rules, checked in this order:
 *   1. Unparseable, or not an object                                  -> malformed
 *   2. `series.points` with a top-level `t` or `v` array              -> malformed
 *      (before the version: a flat `v` array is not a version)
 *   3. `v !== 1`                                                      -> incompatible
 *   4. Unknown `type`                                                 -> ignored
 *   5. `overlay.upsert` with an unknown `kind`                        -> ignored
 *   6. Snapshot: unknown-kind overlays are removed and counted
 *   7. Wrong field type, out-of-enum value, non-finite number,
 *      non-integer time, columns of unequal length                    -> malformed
 *
 * A message with no `v` at all is malformed (every message carries one); a message missing
 * `source` or `seq`, or with a non-integer `seq`, is malformed too.
 */
import type { OverlaySpec, PointCols, StreamMessage } from "./types";

export type DecodeResult =
  | { ok: true; message: StreamMessage; ignoredOverlays: number }
  | { ok: false; reason: "malformed" | "ignored" | "incompatible"; detail: string };

class Malformed extends Error {}

type Obj = Record<string, unknown>;

const fail = (path: string, what: string): never => {
  throw new Malformed(`${path}: ${what}`);
};

const isObj = (x: unknown): x is Obj => typeof x === "object" && x !== null && !Array.isArray(x);

function obj(x: unknown, path: string): Obj {
  return isObj(x) ? x : fail(path, "expected an object");
}

function str(o: Obj, key: string, path: string): string {
  const x = o[key];
  return typeof x === "string" ? x : fail(`${path}.${key}`, "expected a string");
}

function num(o: Obj, key: string, path: string): number {
  const x = o[key];
  return typeof x === "number" && Number.isFinite(x) ? x : fail(`${path}.${key}`, "expected a finite number");
}

/** An integer-valued number: times, `seq`, counts. */
function int(o: Obj, key: string, path: string): number {
  const x = o[key];
  return typeof x === "number" && Number.isInteger(x) ? x : fail(`${path}.${key}`, "expected an integer");
}

function bool(o: Obj, key: string, path: string): boolean {
  const x = o[key];
  return typeof x === "boolean" ? x : fail(`${path}.${key}`, "expected a boolean");
}

function oneOf<T extends string>(o: Obj, key: string, allowed: readonly T[], path: string): T {
  const x = o[key];
  return typeof x === "string" && (allowed as readonly string[]).includes(x)
    ? (x as T)
    : fail(`${path}.${key}`, `expected one of ${allowed.join("|")}`);
}

function numArray(o: Obj, key: string, path: string, integers: boolean): number[] {
  const x = o[key];
  const at = `${path}.${key}`;
  if (!Array.isArray(x)) return fail(at, "expected an array");
  for (let i = 0; i < x.length; i++) {
    const e: unknown = x[i];
    if (typeof e !== "number" || !Number.isFinite(e)) fail(`${at}[${i}]`, "expected a finite number");
    if (integers && !Number.isInteger(e)) fail(`${at}[${i}]`, "expected an integer");
  }
  return x as number[];
}

function sameLength(cols: Record<string, number[]>, path: string): void {
  const lengths = Object.values(cols).map((c) => c.length);
  if (lengths.some((n) => n !== lengths[0])) fail(path, "columns have unequal length");
}

function pointCols(x: unknown, path: string): PointCols {
  const o = obj(x, path);
  const cols = { t: numArray(o, "t", path, true), v: numArray(o, "v", path, false) };
  sameLength(cols, path);
  return cols;
}

const KINDS = ["pane", "band", "marker", "series", "level"] as const;

function isKnownKind(kind: unknown): boolean {
  return typeof kind === "string" && (KINDS as readonly string[]).includes(kind);
}

function overlay(x: unknown, path: string): OverlaySpec {
  const o = obj(x, path);
  str(o, "id", path);
  str(o, "group", path);
  str(o, "pane", path);
  switch (oneOf(o, "kind", KINDS, path)) {
    case "pane":
      str(o, "title", path);
      num(o, "height_px", path);
      num(o, "order", path);
      break;
    case "band":
      int(o, "start_ms", path);
      if (o.end_ms !== null) int(o, "end_ms", path);
      str(o, "color", path);
      str(o, "label", path);
      break;
    case "marker":
      int(o, "time_ms", path);
      oneOf(o, "position", ["above", "below", "inBar"] as const, path);
      oneOf(o, "shape", ["arrowUp", "arrowDown", "circle", "square"] as const, path);
      str(o, "color", path);
      str(o, "text", path);
      break;
    case "series":
      oneOf(o, "style", ["line", "area", "histogram", "step"] as const, path);
      str(o, "color", path);
      num(o, "width", path);
      oneOf(o, "agg", ["last", "mean", "min", "max", "sum"] as const, path);
      num(o, "precision", path);
      break;
    case "level":
      num(o, "value", path);
      str(o, "color", path);
      str(o, "label", path);
      bool(o, "dash", path);
      break;
  }
  return o as unknown as OverlaySpec;
}

/** Snapshot columns seed the stores whole, and the stores and the chart need ascending times. */
function strictlyAscending(t: number[], path: string): void {
  for (let i = 1; i < t.length; i++) {
    if (!(t[i]! > t[i - 1]!)) fail(`${path}[${i}]`, "times must be strictly ascending");
  }
}

function barCols(x: unknown, path: string): void {
  const o = obj(x, path);
  const cols: Record<string, number[]> = { t: numArray(o, "t", path, true) };
  for (const k of ["o", "h", "l", "c", "vol"]) cols[k] = numArray(o, k, path, false);
  sameLength(cols, path);
  strictlyAscending(cols.t!, `${path}.t`);
}

/** Validates the payload of a known `type`. Returns the number of overlays dropped (snapshots only). */
function validate(m: Obj, type: string): { message: StreamMessage; ignoredOverlays: number } | "unknown" {
  switch (type) {
    case "snapshot": {
      str(m, "asset", "snapshot");
      const assets = m.assets;
      if (!Array.isArray(assets)) fail("snapshot.assets", "expected an array");
      (assets as unknown[]).forEach((a, i) => {
        const ao = obj(a, `snapshot.assets[${i}]`);
        int(ao, "id", `snapshot.assets[${i}]`);
        str(ao, "symbol", `snapshot.assets[${i}]`);
      });
      int(m, "session_start_ms", "snapshot");
      barCols(m.bars, "snapshot.bars");
      if (!Array.isArray(m.overlays)) fail("snapshot.overlays", "expected an array");
      const kept: OverlaySpec[] = [];
      let ignored = 0;
      (m.overlays as unknown[]).forEach((x, i) => {
        const path = `snapshot.overlays[${i}]`;
        // An unknown kind is dropped, but only a well-formed object can be told apart from garbage.
        if (isObj(x) && typeof x.kind === "string" && !isKnownKind(x.kind)) ignored++;
        else kept.push(overlay(x, path));
      });
      const points = obj(m.points, "snapshot.points");
      // A `series.points` message may repeat or reorder times: `OverlayStore.appendPoints` sorts that out.
      for (const [id, cols] of Object.entries(points)) {
        strictlyAscending(pointCols(cols, `snapshot.points[${id}]`).t, `snapshot.points[${id}].t`);
      }
      return { message: { ...m, overlays: kept } as unknown as StreamMessage, ignoredOverlays: ignored };
    }
    case "bar":
      int(m, "t", "bar");
      for (const k of ["o", "h", "l", "c", "vol"]) num(m, k, "bar");
      break;
    case "overlay.upsert": {
      overlay(m.overlay, "overlay.upsert.overlay");
      break;
    }
    case "overlay.remove":
      str(m, "id", "overlay.remove");
      break;
    case "series.points":
      str(m, "id", "series.points");
      pointCols(m.points, "series.points.points");
      break;
    case "heartbeat":
      int(m, "now_ms", "heartbeat");
      break;
    case "status":
      str(m, "feed_state", "status");
      int(m, "drop_count", "status");
      if (m.message !== undefined) str(m, "message", "status");
      break;
    default:
      return "unknown";
  }
  return { message: m as unknown as StreamMessage, ignoredOverlays: 0 };
}

export function decodeMessage(raw: string): DecodeResult {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { ok: false, reason: "malformed", detail: "not valid JSON" };
  }
  if (!isObj(parsed)) return { ok: false, reason: "malformed", detail: "not a JSON object" };
  const m = parsed;

  try {
    if (m.type === "series.points" && (Array.isArray(m.t) || Array.isArray(m.v))) {
      return { ok: false, reason: "malformed", detail: "series.points carries flat t/v arrays; points must be nested under `points`" };
    }
    if (!("v" in m)) return { ok: false, reason: "malformed", detail: "missing envelope version `v`" };
    if (m.v !== 1) return { ok: false, reason: "incompatible", detail: `unsupported protocol version ${JSON.stringify(m.v)}` };
    str(m, "source", "envelope");
    int(m, "seq", "envelope");
    const type = str(m, "type", "envelope");

    // An upsert of an overlay kind this client does not know is skipped, not an error.
    if (type === "overlay.upsert" && isObj(m.overlay) && typeof m.overlay.kind === "string" && !isKnownKind(m.overlay.kind)) {
      return { ok: false, reason: "ignored", detail: `unknown overlay kind ${JSON.stringify(m.overlay.kind)}` };
    }
    const result = validate(m, type);
    if (result === "unknown") return { ok: false, reason: "ignored", detail: `unknown message type ${JSON.stringify(type)}` };
    return { ok: true, ...result };
  } catch (e) {
    if (e instanceof Malformed) return { ok: false, reason: "malformed", detail: e.message };
    throw e;
  }
}
