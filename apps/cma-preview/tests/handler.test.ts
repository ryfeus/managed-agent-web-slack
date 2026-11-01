import { it, expect, vi, afterEach } from "vitest";
import { handler } from "../src/index.js";
import { compose } from "../src/composition.js";
import { observe } from "../src/observer.js";
import type { SQSEvent, Context } from "aws-lambda";

vi.mock("../src/composition.js", () => ({ compose: vi.fn(async () => ({})) }));
vi.mock("../src/observer.js", async original => ({ ...await original<object>(), observe: vi.fn(async () => {}) }));
const context = { getRemainingTimeInMillis: () => 130000 } as Context;
function event(receives = "1", age = 0): SQSEvent {
  return { Records: [{ attributes: { ApproximateReceiveCount: receives }, body: JSON.stringify({ v: 1, taskId: "task", contextId: "context", sessionId: "private", phaseId: "phase", createdAt: Date.now() - age }) }] } as SQSEvent;
}
afterEach(() => { vi.unstubAllEnvs(); vi.clearAllMocks(); });
it("disabled preview never constructs credentials or provider clients", async () => {
  vi.stubEnv("PREVIEW_ENABLED", "false"); await handler(event(), context);
  expect(compose).not.toHaveBeenCalled(); expect(observe).not.toHaveBeenCalled();
});
it("acknowledges stale SQS delivery and retries without another reader", async () => {
  vi.stubEnv("PREVIEW_ENABLED", "true");
  await handler(event("2"), context); await handler(event("1", 31000), context);
  expect(compose).not.toHaveBeenCalled();
  await handler(event(), context); expect(observe).toHaveBeenCalledTimes(1);
});
it("acknowledges preview composition failure without propagating a P0 retry", async () => {
  vi.stubEnv("PREVIEW_ENABLED", "true"); vi.mocked(compose).mockRejectedValueOnce(new Error("do not log private body"));
  await expect(handler(event(), context)).resolves.toBeUndefined(); expect(observe).not.toHaveBeenCalled();
});
