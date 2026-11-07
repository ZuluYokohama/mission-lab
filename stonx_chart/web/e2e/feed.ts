import { spawn, type ChildProcess } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

// package.json is "type": "module", so __dirname does not exist.
const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "../../..");

export interface FeedOptions {
  port: number;
  speed: number;
  backfill: string;
  history?: string;
  /** How long to wait for /api/health before giving up (default 60 s; long backfills need more). */
  healthTimeoutMs?: number;
}

export interface Feed {
  url: string;
  stop(): Promise<void>;
}

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

async function healthy(url: string): Promise<boolean> {
  try {
    const res = await fetch(`${url}/api/health`, { signal: AbortSignal.timeout(1000) });
    return res.ok;
  } catch {
    return false;
  }
}

/** Starts the loopback feed and resolves once /api/health answers; the child is killed on any failure. */
export async function startFeed(o: FeedOptions): Promise<Feed> {
  const url = `http://127.0.0.1:${o.port}`;
  if (await healthy(url)) throw new Error(`port ${o.port} is already serving a feed; refusing to reuse it`);

  const args = ["-B", "-m", "stonx_chart.feed", "--seed", "7", "--speed", String(o.speed), "--backfill", o.backfill, "--port", String(o.port)];
  if (o.history !== undefined) args.push("--history", o.history);

  const child: ChildProcess = spawn(process.env.STONX_PYTHON ?? "python", args, {
    cwd: REPO_ROOT,
    stdio: ["ignore", "ignore", "pipe"],
    windowsHide: true,
  });
  let stderr = "";
  child.stderr?.on("data", (d: Buffer) => {
    stderr = (stderr + d.toString()).slice(-8000);
  });
  let exited = false;
  let spawnError: Error | null = null;
  const exit = new Promise<void>((resolve) => {
    child.once("exit", () => {
      exited = true;
      resolve();
    });
    child.once("error", (e) => {
      spawnError = e;
      exited = true;
      resolve();
    });
  });

  const stop = async (): Promise<void> => {
    if (!exited) {
      child.kill();
      await Promise.race([exit, sleep(10_000)]);
    }
    // Wait for the OS to release the port so a restart on the same port can bind it.
    const deadline = Date.now() + 10_000;
    while (Date.now() < deadline && (await healthy(url))) await sleep(100);
  };

  const timeoutMs = o.healthTimeoutMs ?? 60_000;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (exited) {
      throw new Error(`feed exited before becoming healthy${spawnError ? `: ${spawnError}` : ""}\n--- feed stderr ---\n${stderr}`);
    }
    if (await healthy(url)) return { url, stop };
    await sleep(100);
  }
  await stop();
  throw new Error(`feed on port ${o.port} not healthy after ${timeoutMs} ms\n--- feed stderr ---\n${stderr}`);
}
