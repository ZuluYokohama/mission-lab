import { expect, test, vi } from "vitest";
import { createErrorReporter, runFrame } from "../src/app/frameLoop";

test("a throwing step does not stop later steps", () => {
  const ran: string[] = [];
  const errors: unknown[] = [];
  runFrame(
    [
      () => ran.push("a"),
      () => {
        throw new Error("boom");
      },
      () => ran.push("c"),
    ],
    (e) => errors.push(e),
  );
  expect(ran).toEqual(["a", "c"]);
  expect(errors).toHaveLength(1);
});

test("repeated identical errors are reported once, distinct ones each once", () => {
  const log = vi.fn();
  const report = createErrorReporter(log);
  const failing = (m: string) => () => {
    throw new Error(m);
  };
  for (let i = 0; i < 5; i++) runFrame([failing("same"), failing("other")], report);
  runFrame([failing("third")], report);
  expect(log).toHaveBeenCalledTimes(3);
  report("not an error");
  report("not an error");
  expect(log).toHaveBeenCalledTimes(4);
});
