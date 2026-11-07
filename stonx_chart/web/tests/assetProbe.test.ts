import { expect, test } from "vitest";
import { streamRejected } from "../src/transport/assetProbe";

const answering =
  (status: number) =>
  async (_url: string | URL | Request, init?: RequestInit): Promise<Response> => {
    expect(init?.signal?.aborted).toBe(false);
    return new Response(null, { status });
  };

test("only HTTP 400 counts as a rejected stream", async () => {
  expect(await streamRejected("/api/stream?asset=NOPE", answering(400) as typeof fetch)).toBe(true);
  expect(await streamRejected("/api/stream?asset=AMD", answering(200) as typeof fetch)).toBe(false);
  expect(await streamRejected("/api/stream?asset=AMD", answering(500) as typeof fetch)).toBe(false);
});

test("a network failure is not a rejection", async () => {
  const down = (async () => {
    throw new TypeError("fetch failed");
  }) as unknown as typeof fetch;
  expect(await streamRejected("/api/stream?asset=AMD", down)).toBe(false);
});

test("the probe aborts its request once it has the status", async () => {
  let signal: AbortSignal | undefined;
  const f = (async (_u: string, init?: RequestInit) => {
    signal = init?.signal ?? undefined;
    return new Response(null, { status: 200 });
  }) as unknown as typeof fetch;
  await streamRejected("/x", f);
  expect(signal?.aborted).toBe(true);
});
