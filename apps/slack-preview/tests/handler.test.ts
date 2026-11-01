import { it, expect, vi, afterEach } from "vitest";
import type { SQSEvent, Context } from "aws-lambda";
import { handler } from "../src/index.js";
import { compose } from "../src/composition.js";
import { observe } from "../src/worker.js";
vi.mock("../src/composition.js", () => ({ compose: vi.fn(async () => ({ close: vi.fn() })) }));
vi.mock("../src/worker.js", async original => ({ ...await original<object>(), observe: vi.fn() }));
const bindingId = "11111111-1111-4111-8111-111111111111";
const context = { getRemainingTimeInMillis: () => 130000 } as Context;
const event = (receive = "1", age = 0, group = bindingId) => ({ Records: [{
  body: JSON.stringify({ v: 1, bindingId, taskId: "task", createdAt: Date.now() - age }),
  attributes: { ApproximateReceiveCount: receive, MessageGroupId: group }
}] }) as SQSEvent;
afterEach(() => { vi.unstubAllEnvs(); vi.clearAllMocks(); });
it.each([["false", "true"], ["true", "false"]])("requires both flags before loading any credentials", async (slack, producer) => {
  vi.stubEnv("SLACK_PREVIEW_ENABLED", slack); vi.stubEnv("PREVIEW_ENABLED", producer);
  await handler(event(), context); expect(compose).not.toHaveBeenCalled();
});
it("drops stale, repeated and wrong-group hints; acknowledges failure without P0 work", async () => {
  vi.stubEnv("SLACK_PREVIEW_ENABLED", "true"); vi.stubEnv("PREVIEW_ENABLED", "true");
  await handler(event("2"), context); await handler(event("1", 31000), context); await handler(event("1", 0, "wrong"), context);
  expect(compose).not.toHaveBeenCalled();
  vi.mocked(compose).mockRejectedValueOnce(new Error("secret body"));
  await expect(handler(event(), context)).resolves.toBeUndefined();
  expect(observe).not.toHaveBeenCalled();
  await handler(event(), context); expect(observe).toHaveBeenCalledTimes(1);
});
