import { expect, test } from "vitest";
import { DEFAULT_PREFS, PREFS_KEY, loadPrefs, savePrefs, type Prefs } from "../src/ui/prefs";

const store = (value: string | null) => ({ getItem: (k: string) => (k === PREFS_KEY ? value : null) });

test("loadPrefs falls back on throwing storage", () => {
  expect(
    loadPrefs({
      getItem: () => {
        throw new Error("blocked");
      },
    }),
  ).toEqual(DEFAULT_PREFS);
  expect(loadPrefs(null)).toEqual(DEFAULT_PREFS);
});

test("loadPrefs falls back on bad data", () => {
  expect(loadPrefs(store("not json"))).toEqual(DEFAULT_PREFS);
  expect(loadPrefs(store(null))).toEqual(DEFAULT_PREFS);
  expect(loadPrefs(store("null"))).toEqual(DEFAULT_PREFS);
  expect(loadPrefs(store(JSON.stringify({ asset: "AMD", interval: 7 })))).toEqual({ ...DEFAULT_PREFS, asset: "AMD" });
  expect(loadPrefs(store(JSON.stringify({ asset: "", hiddenGroups: ["a", 3] })))).toEqual(DEFAULT_PREFS);
});

test("prefs round trip and setItem errors swallowed", () => {
  const p: Prefs = { asset: "AMD", interval: 5, hiddenGroups: ["lambda2"] };
  const saved: Record<string, string> = {};
  savePrefs({ setItem: (k, v) => void (saved[k] = v) }, p);
  expect(loadPrefs({ getItem: (k) => saved[k] ?? null })).toEqual(p);
  expect(() =>
    savePrefs(
      {
        setItem: () => {
          throw new Error("quota");
        },
      },
      p,
    ),
  ).not.toThrow();
  expect(() => savePrefs(null, p)).not.toThrow();
});
