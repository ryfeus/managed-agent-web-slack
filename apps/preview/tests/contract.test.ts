import { it, expect } from "vitest";
import { PreviewAccumulator, parsePreview, type Preview } from "../src/contract.js";
const base = { v: 1 as const, taskId: "task", previewId: "a".repeat(64) };
it("suppresses duplicates, rejects gaps and never admits foreign/private payloads", () => {
  const a = new PreviewAccumulator();
  expect(a.accept({ ...base, type: "start", offset: 0 })?.start).toBe(true);
  const delta: Preview = { ...base, type: "delta", offset: 1, text: "hello" };
  expect(a.accept(delta)?.text).toBe("hello"); expect(a.accept(delta)).toBeUndefined();
  expect(a.accept({ ...delta, offset: 3 })).toBeUndefined(); expect(a.accept({ ...delta, offset: 2 })).toBeUndefined();
  expect(parsePreview({ ...delta, sessionId: "private" }, "task")).toBeUndefined();
  expect(parsePreview(delta, "foreign")).toBeUndefined();
  expect(parsePreview({ ...delta, text: "x".repeat(9000) }, "task")).toBeUndefined();
});
