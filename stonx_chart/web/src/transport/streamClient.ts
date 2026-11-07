/**
 * Owns one EventSource at a time: decodes each message, detects a silent stream, reconnects
 * with backoff, and ignores late events from any connection that has been replaced or closed.
 *
 * Every connection gets a generation number; handlers and timers capture it and do nothing once
 * it is no longer current. That is what keeps an old asset's late event or pending reconnect
 * timer from opening a stream or leaking data after `open()` switches asset.
 */
import { decodeMessage } from "../protocol/decode";
import type { StreamMessage } from "../protocol/types";

export type ConnState = "connecting" | "live" | "stale" | "reconnecting" | "incompatible" | "closed";

export interface EventSourceLike {
  onmessage: ((e: { data: string }) => void) | null;
  onerror: (() => void) | null;
  onopen: (() => void) | null;
  close(): void;
}

export interface StreamClientOptions {
  url: (asset: string) => string;
  onMessage: (m: StreamMessage, ignoredOverlays: number) => void;
  onDecodeIssue: (r: "malformed" | "ignored") => void;
  onState: (s: ConnState) => void;
  /** Called each time a new connection is created, before any of its messages. */
  onConnection: () => void;
  createEventSource?: (url: string) => EventSourceLike;
  staleAfterMs?: number;
}

const DEFAULT_STALE_MS = 3000;
const BACKOFF_START_MS = 500;
const BACKOFF_CAP_MS = 5000;

export class StreamClient {
  private readonly o: StreamClientOptions;
  private readonly staleAfterMs: number;
  private current: ConnState = "closed";
  private source: EventSourceLike | null = null;
  private asset: string | null = null;
  private generation = 0;
  private backoff = BACKOFF_START_MS;
  private staleTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(o: StreamClientOptions) {
    this.o = o;
    this.staleAfterMs = o.staleAfterMs ?? DEFAULT_STALE_MS;
  }

  get state(): ConnState {
    return this.current;
  }

  /** Starts streaming `asset`, replacing any current connection. A new asset is a fresh start. */
  open(asset: string): void {
    this.asset = asset;
    this.backoff = BACKOFF_START_MS;
    this.connect();
  }

  close(): void {
    this.teardown();
    this.asset = null;
    this.setState("closed");
  }

  private setState(s: ConnState): void {
    if (s === this.current) return;
    this.current = s;
    this.o.onState(s);
  }

  /** Cancels timers, closes the source and invalidates (by generation) every handler of the old connection. */
  private teardown(): void {
    this.clearTimers();
    this.generation++;
    const src = this.source;
    this.source = null;
    // Handlers stay attached on purpose: their generation check is the guard, and it is what
    // the tests exercise by firing a closed source's late events.
    src?.close();
  }

  private clearTimers(): void {
    if (this.staleTimer !== null) clearTimeout(this.staleTimer);
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer);
    this.staleTimer = null;
    this.reconnectTimer = null;
  }

  /** Shared by `open()` and the reconnect timer; does not touch the backoff. */
  private connect(): void {
    if (this.asset === null) return;
    this.teardown();
    const gen = this.generation;
    const create =
      this.o.createEventSource ?? ((url: string): EventSourceLike => new EventSource(url) as unknown as EventSourceLike);
    const src = create(this.o.url(this.asset));
    this.source = src;
    this.o.onConnection();
    this.setState("connecting");
    src.onmessage = (e) => {
      if (gen === this.generation) this.receive(e.data);
    };
    src.onerror = () => {
      if (gen === this.generation) this.failConnection();
    };
    this.armStaleTimer(gen);
  }

  private armStaleTimer(gen: number): void {
    if (this.staleTimer !== null) clearTimeout(this.staleTimer);
    this.staleTimer = setTimeout(() => {
      if (gen !== this.generation) return;
      this.setState("stale");
      this.failConnection();
    }, this.staleAfterMs);
  }

  private receive(raw: string): void {
    const gen = this.generation;
    const result = decodeMessage(raw);
    if (!result.ok && result.reason === "incompatible") {
      this.teardown();
      this.setState("incompatible");
      return;
    }
    // Any other message, even a malformed or ignored one, proves the connection is alive.
    this.armStaleTimer(gen);
    if (!result.ok) {
      this.o.onDecodeIssue(result.reason as "malformed" | "ignored");
      return;
    }
    if (result.message.type === "snapshot") {
      this.backoff = BACKOFF_START_MS;
      this.setState("live");
    }
    this.o.onMessage(result.message, result.ignoredOverlays);
  }

  /** Closes the current source and schedules a reopen of the same asset after the backoff. */
  private failConnection(): void {
    this.teardown();
    this.setState("reconnecting");
    const gen = this.generation;
    const delay = this.backoff;
    this.backoff = Math.min(this.backoff * 2, BACKOFF_CAP_MS);
    this.reconnectTimer = setTimeout(() => {
      if (gen === this.generation) this.connect();
    }, delay);
  }
}
