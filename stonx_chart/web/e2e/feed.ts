import { spawn, spawnSync, type ChildProcess } from "node:child_process";
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
    // "close", not "exit": by then stderr has been fully read, so failure messages carry all of it.
    child.once("close", () => {
      exited = true;
      resolve();
    });
    child.once("error", (e) => {
      spawnError = e;
      exited = true;
      resolve();
    });
  });

  /** True while the child has not exited; a pid is only safe to signal until then (afterwards it may be reused). */
  const running = (): boolean => !exited && child.exitCode === null && child.signalCode === null;
  /** Kills a running child and its descendants (a Windows venv python.exe can be a launcher whose child lives on). */
  const killTree = (): void => {
    if (child.pid === undefined || !running()) return;
    if (process.platform === "win32") spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true });
    else child.kill("SIGKILL");
  };
  const portClosed = async (ms: number): Promise<boolean> => {
    const until = Date.now() + ms;
    while (Date.now() < until) {
      if (!(await healthy(url))) return true;
      await sleep(100);
    }
    return !(await healthy(url));
  };

  const stop = async (): Promise<void> => {
    if (process.platform === "win32") killTree(); // child.kill() would leave a launcher's child holding the port
    else if (running()) child.kill();
    await Promise.race([exit, sleep(5_000)]);
    // Escalate if the process is still around or the port is still held (killTree only acts on a running child).
    if (running() || !(await portClosed(5_000))) {
      killTree();
      await Promise.race([exit, sleep(5_000)]);
    }
    if (!(await portClosed(10_000))) throw new Error(`feed on port ${o.port} still answers after being killed`);
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
