import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { StreamClient, type ConnState, type EventSourceLike } from "../src/transport/streamClient";
import type { StreamMessage } from "../src/protocol/types";

const env = { v: 1, source: "SIMULATED_DEMO", seq: 1 };
const SNAPSHOT = JSON.stringify({
  ...env,
  type: "snapshot",
  asset: "NVDA",
  assets: [{ id: 0, symbol: "NVDA" }],
  session_start_ms: 1000,
  bars: { t: [1000], o: [1], h: [2], l: [0.5], c: [1.5], vol: [10] },
  overlays: [],
  points: {},
});
const HEARTBEAT = JSON.stringify({ ...env, seq: 3, type: "heartbeat", now_ms: 2000 });
const V2 = JSON.stringify({ v: 2, source: "X", seq: 1, type: "heartbeat" });
const UNKNOWN_TYPE = JSON.stringify({ ...env, type: "from.the.future" });

class FakeSource implements EventSourceLike {
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  closed = false;
  constructor(readonly url: string) {}
  close(): void {
    this.closed = true;
  }
}

let sources: FakeSource[];
let states: ConnState[];
let c: StreamClient;
let onMessage: ReturnType<typeof vi.fn<(m: StreamMessage, ignored: number) => void>>;
let onDecodeIssue: ReturnType<typeof vi.fn<(r: "malformed" | "ignored") => void>>;
let onConnection: ReturnType<typeof vi.fn<() => void>>;

const last = (): FakeSource => sources[sources.length - 1]!;

beforeEach(() => {
  vi.useFakeTimers();
  sources = [];
  states = [];
  onMessage = vi.fn();
  onDecodeIssue = vi.fn();
  onConnection = vi.fn();
  c = new StreamClient({
    url: (asset) => `/api/stream?asset=${asset}`,
    onMessage,
    onDecodeIssue,
    onState: (s) => states.push(s),
    onConnection,
    createEventSource: (url) => {
      const s = new FakeSource(url);
      sources.push(s);
      return s;
    },
  });
});

afterEach(() => {
  c.close();
  vi.useRealTimers();
});

/** Lets the current source go stale, then checks the reconnect waits exactly `delay` ms. */
function failAndReconnect(delay: number): void {
  const before = sources.length;
  vi.advanceTimersByTime(3000);
  expect(c.state).toBe("reconnecting");
  vi.advanceTimersByTime(delay - 1);
  expect(sources.length).toBe(before);
  vi.advanceTimersByTime(1);
  expect(sources.length).toBe(before + 1);
}

describe("StreamClient", () => {
  test("opens, goes live on snapshot", () => {
    c.open("NVDA");
    expect(sources.map((s) => s.url)).toEqual(["/api/stream?asset=NVDA"]);
    expect(c.state).toBe("connecting");
    last().onmessage!({ data: SNAPSHOT });
    expect(c.state).toBe("live");
    expect(onConnection).toHaveBeenCalledTimes(1);
    expect(onMessage).toHaveBeenCalledTimes(1);
    expect(onMessage.mock.calls[0]![0].type).toBe("snapshot");
    expect(onMessage.mock.calls[0]![1]).toBe(0);
  });

  test("onState fires only on actual changes", () => {
    c.open("NVDA");
    last().onmessage!({ data: SNAPSHOT });
    last().onmessage!({ data: HEARTBEAT });
    last().onmessage!({ data: HEARTBEAT });
    expect(states).toEqual(["connecting", "live"]);
  });

  test("stale after 3 s then backoff 500, 1000 ... capped at 5000", () => {
    c.open("NVDA");
    last().onmessage!({ data: SNAPSHOT });
    const first = last();
    vi.advanceTimersByTime(2999);
    expect(c.state).toBe("live");
    vi.advanceTimersByTime(1);
    expect(states).toContain("stale");
    expect(c.state).toBe("reconnecting");
    expect(first.closed).toBe(true);
    vi.advanceTimersByTime(499);
    expect(sources.length).toBe(1);
    vi.advanceTimersByTime(1);
    expect(sources.length).toBe(2);
    expect(c.state).toBe("connecting");
    expect(onConnection).toHaveBeenCalledTimes(2);
    for (const delay of [1000, 2000, 4000, 5000, 5000]) failAndReconnect(delay);
  });

  test("every message restarts the stale timer", () => {
    c.open("NVDA");
    last().onmessage!({ data: SNAPSHOT });
    vi.advanceTimersByTime(2000);
    last().onmessage!({ data: HEARTBEAT });
    vi.advanceTimersByTime(2000);
    expect(c.state).toBe("live");
    vi.advanceTimersByTime(1000);
    expect(c.state).toBe("reconnecting");
  });

  test("backoff resets after snapshot", () => {
    c.open("NVDA");
    failAndReconnect(500);
    failAndReconnect(1000);
    last().onmessage!({ data: SNAPSHOT });
    expect(c.state).toBe("live");
    failAndReconnect(500);
  });

  test("onerror reconnects", () => {
    c.open("NVDA");
    const first = last();
    first.onerror!();
    expect(first.closed).toBe(true);
    expect(c.state).toBe("reconnecting");
    vi.advanceTimersByTime(499);
    expect(sources.length).toBe(1);
    vi.advanceTimersByTime(1);
    expect(sources.length).toBe(2);
    expect(last().url).toBe("/api/stream?asset=NVDA");
  });

  test("incompatible version stops reconnecting", () => {
    c.open("NVDA");
    last().onmessage!({ data: V2 });
    expect(c.state).toBe("incompatible");
    expect(last().closed).toBe(true);
    vi.advanceTimersByTime(60_000);
    expect(sources.length).toBe(1);
    expect(c.state).toBe("incompatible");
    expect(onMessage).not.toHaveBeenCalled();
  });

  test("malformed reported, connection kept", () => {
    c.open("NVDA");
    last().onmessage!({ data: SNAPSHOT });
    vi.advanceTimersByTime(2500);
    last().onmessage!({ data: "nope" });
    expect(onDecodeIssue).toHaveBeenCalledWith("malformed");
    expect(c.state).toBe("live");
    expect(last().closed).toBe(false);
    vi.advanceTimersByTime(2500);
    expect(c.state).toBe("live");
    expect(onMessage).toHaveBeenCalledTimes(1);
  });

  test("ignored message reports the issue and keeps the connection alive", () => {
    c.open("NVDA");
    last().onmessage!({ data: SNAPSHOT });
    vi.advanceTimersByTime(2500);
    last().onmessage!({ data: UNKNOWN_TYPE });
    expect(onDecodeIssue).toHaveBeenCalledWith("ignored");
    vi.advanceTimersByTime(2500);
    expect(c.state).toBe("live");
  });

  test("asset switch cancels pending reconnect and ignores old stream", () => {
    c.open("NVDA");
    const old = last();
    old.onerror!();
    expect(c.state).toBe("reconnecting");
    c.open("AMD");
    expect(c.state).toBe("connecting");
    vi.advanceTimersByTime(10_000);
    const urls = sources.map((s) => s.url);
    expect(urls.filter((u) => u.includes("NVDA"))).toHaveLength(1);
    // the silent AMD stream went stale and reconnected on its own; every later source is AMD
    expect(urls.slice(1).every((u) => u === "/api/stream?asset=AMD")).toBe(true);
    old.onmessage!({ data: SNAPSHOT });
    old.onerror!();
    expect(onMessage).not.toHaveBeenCalled();
    expect(onDecodeIssue).not.toHaveBeenCalled();
    expect(sources.filter((s) => s.url.includes("NVDA"))).toHaveLength(1);
  });

  test("open resets backoff and ignores late events of the replaced source", () => {
    c.open("NVDA");
    failAndReconnect(500);
    failAndReconnect(1000);
    const prev = last();
    c.open("AMD");
    expect(prev.closed).toBe(true);
    prev.onmessage!({ data: SNAPSHOT });
    expect(onMessage).not.toHaveBeenCalled();
    failAndReconnect(500);
  });

  test("close sets closed and clears timers", () => {
    c.open("NVDA");
    last().onerror!();
    c.close();
    expect(c.state).toBe("closed");
    vi.advanceTimersByTime(60_000);
    expect(sources.length).toBe(1);
    c.open("NVDA");
    expect(sources.length).toBe(2);
    c.close();
    expect(last().closed).toBe(true);
  });
});
