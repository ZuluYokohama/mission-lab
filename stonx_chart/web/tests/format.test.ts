import { expect, test } from "vitest";
import { formatAge, formatNumber, formatTick, formatTime } from "../src/ui/format";

test("formats in New York time across DST", () => {
  expect(formatTime(1_793_975_400_000, true)).toBe("09:30:00");
  expect(formatTime(1_782_912_600_000, true)).toBe("09:30:00");
  expect(formatTime(1_793_975_400_000, false)).toBe("09:30");
});

test("formatAge", () => {
  expect(formatAge(null)).toBe("—");
  expect(formatAge(400)).toBe("0.4 s");
  expect(formatAge(12_000)).toBe("12 s");
  expect(formatAge(180_000)).toBe("3 min");
});

test("formatTick shows seconds only below 60 s intervals", () => {
  expect(formatTick(1_793_975_400_000, 1)).toBe("09:30:00");
  expect(formatTick(1_793_975_400_000, 5)).toBe("09:30:00");
  expect(formatTick(1_793_975_400_000, 60)).toBe("09:30");
  expect(formatTick(1_793_975_400_000, 300)).toBe("09:30");
});

test("formatNumber", () => {
  expect(formatNumber(1.23456, 2)).toBe("1.23");
  expect(formatNumber(100, 0)).toBe("100");
  expect(formatNumber(Number.NaN, 2)).toBe("—");
  expect(formatNumber(1.5, 150)).toBe((1.5).toFixed(20)); // out-of-range precision is clamped, not thrown
  expect(formatNumber(1.5, -3)).toBe("2");
  expect(formatNumber(1.234, 1.9)).toBe("1.2");
});
