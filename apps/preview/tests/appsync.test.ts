import { it, expect } from "vitest";
import { setTimeout as sleep } from "node:timers/promises";
import { appSync, type Preview } from "../src/index.js";
import { fakeAppSync } from "../testing/fake.js";

it.each([false, true])("does handshake, fanout and cleanup with batch frames %s", async batchFrames => {
  const fake = await fakeAppSync();
  fake.state.batchFrames = batchFrames;
  const transport = appSync({ ...fake, region: "us-west-2", local: true });
  const abort = new AbortController(), left: Preview[] = [], right: Preview[] = [];
  const a = transport.subscribe("task", p => left.push(p), abort.signal);
  const b = transport.subscribe("task", p => right.push(p), abort.signal);
  try {
    await expect.poll(() => fake.subscriptions.size).toBe(2);
    const p: Preview = { v: 1, taskId: "task", previewId: "f".repeat(64), offset: 0, type: "start" };
    await transport.publish([p], abort.signal);
    await expect.poll(() => left.length + right.length).toBe(2);
    expect(left).toEqual(right);
    await transport.publish([{ ...p, taskId: "foreign" }], abort.signal); await sleep(20);
    expect(left.length).toBe(1);
    fake.state.failPublish = true;
    await expect(transport.publish([p], abort.signal)).rejects.toThrow("unavailable");
    abort.abort(); await Promise.all([a, b]);
    await expect.poll(() => fake.subscriptions.size).toBe(0);
  } finally { abort.abort(); await fake.close(); }
});

it("subscription failure closes without any canonical dependency", async () => {
  const fake = await fakeAppSync(); fake.state.rejectSubscribe = true;
  try {
    await appSync({ ...fake, region: "us-west-2", local: true }).subscribe("task", () => { throw new Error("No preview expected"); }, new AbortController().signal);
    expect(fake.subscriptions.size).toBe(0);
  } finally { await fake.close(); }
});
