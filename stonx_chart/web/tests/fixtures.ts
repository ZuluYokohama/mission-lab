import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const WIRE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "../../fixtures/wire");

/** Wire messages from a golden fixture, one JSON string each. A JSON array yields one string per element, an object one string. */
export function loadFixtureMessages(name: string): string[] {
  const parsed: unknown = JSON.parse(readFileSync(resolve(WIRE_DIR, name), "utf8"));
  if (Array.isArray(parsed)) return parsed.map((m) => JSON.stringify(m));
  return [JSON.stringify(parsed)];
}
