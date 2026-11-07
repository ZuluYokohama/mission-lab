import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, test } from "vitest";

const WEB = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path: string): string => readFileSync(resolve(WEB, path), "utf8");
const pkg = (name: string): { version: string; license: string } => JSON.parse(read(`node_modules/${name}/package.json`));

// The built page ships public/third-party-licenses.txt; a dependency bump must update it.
describe("third-party-licenses.txt", () => {
  const text = read("public/third-party-licenses.txt");

  test("names the installed versions and licenses", () => {
    for (const name of ["lightweight-charts", "fancy-canvas"]) {
      const { version, license } = pkg(name);
      expect(text).toContain(`${name} ${version}\n`);
      expect(text).toContain(`License: ${license} `);
    }
  });

  test("carries the shipped Apache license verbatim and the MIT permission notice", () => {
    expect(text).toContain(read("node_modules/lightweight-charts/LICENSE"));
    expect(text).toContain("Copyright (c) 2019 TradingView, Inc");
    expect(text).toContain("The above copyright notice and this permission notice shall be included in all");
  });

  test("ends with the complete MIT section", () => {
    // From "MIT License" to the end: the standard MIT text with fancy-canvas's upstream
    // copyright line. A pinned digest, so a dropped grant or warranty paragraph fails.
    const mit = text.slice(text.indexOf("MIT License\n"));
    expect(createHash("sha256").update(mit).digest("hex")).toBe("4ef9ad68ec0a251ea26e699f0647d756885a5750dce74a0aeabcbc50c8255249");
  });

  test("is linked from the page", () => expect(read("index.html")).toContain('href="/third-party-licenses.txt"'));
});
