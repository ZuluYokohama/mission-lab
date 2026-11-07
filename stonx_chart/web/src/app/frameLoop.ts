/** Frame helpers: one throwing step must not stop later steps or the next frame. */

/** Runs every step in order; a step that throws is reported and the rest still run. */
export function runFrame(steps: Array<() => void>, report: (e: unknown) => void): void {
  for (const step of steps) {
    try {
      step();
    } catch (e) {
      report(e);
    }
  }
}

/** A reporter that passes each distinct error message to `log` at most once. */
export function createErrorReporter(log: (e: unknown) => void): (e: unknown) => void {
  const seen = new Set<string>();
  return (e) => {
    const key = e instanceof Error ? e.message : String(e);
    if (seen.has(key)) return;
    seen.add(key);
    log(e);
  };
}
