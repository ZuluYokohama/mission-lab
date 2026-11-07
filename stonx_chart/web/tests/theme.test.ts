import { expect, test } from "vitest";
import { resolveColor, TOKENS } from "../src/surface/theme";

test("every token resolves in both schemes and differs from neutral", () => {
  for (const t of TOKENS.filter((t) => t !== "neutral"))
    for (const s of ["light", "dark"] as const) expect(resolveColor(t, s)).not.toBe(resolveColor("neutral", s));
});

test("unknown token falls back to neutral", () => expect(resolveColor("nope", "dark")).toBe(resolveColor("neutral", "dark")));

test("alpha applied", () => expect(resolveColor("state.halt", "light", 0.18)).toMatch(/^rgba\(\d+, \d+, \d+, 0\.18\)$/));
