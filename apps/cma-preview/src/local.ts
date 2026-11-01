// Local queue/broker composition, launched and reaped only by scripts/local.py.
import { setTimeout as sleep } from "node:timers/promises";
import { fakeAppSync } from "../../preview/testing/fake.js";
import { compose } from "./composition.js";
import { observe, parseHint, type Hint } from "./observer.js";
import { compose as composeSlack } from "../../slack-preview/src/composition.js";
import { observe as observeSlack, parseHint as parseSlackHint } from "../../slack-preview/src/worker.js";
if (process.env.APP_ENV !== "e2e") throw new Error("Local preview fixture only");
const backend = new URL(process.env.LOCAL_BACKEND_URL!);
if (backend.hostname !== "127.0.0.1") throw new Error("Loopback fixture only");
const abort = new AbortController();
const fake = await fakeAppSync(Number(process.env.LOCAL_PREVIEW_PORT));
for (const event of ["SIGTERM", "SIGINT"] as const) process.once(event, () => abort.abort());
const active = new Map<string, Promise<void>>(), pending: Hint[] = [], seen = new Map<string, number>();
const slackActive = new Map<string, Promise<void>>(), slackSeen = new Map<string, number>();
try {
  while (!abort.signal.aborted) {
    try {
      const response = await fetch(new URL("/_test/preview-hints", backend), { signal: AbortSignal.any([abort.signal, AbortSignal.timeout(1000)]) });
      const hints = await response.json() as unknown[];
      for (const raw of hints) {
        const h = parseHint(JSON.stringify(raw)); if (!h) continue;
        const key = `${h.taskId}:${h.phaseId}`;
        if (!seen.has(key)) { seen.set(key, Date.now()); pending.push(h); }
      }
      for (const [key, at] of seen) if (Date.now() - at > 300000) seen.delete(key);
      if (process.env.SLACK_PREVIEW_ENABLED === "true" && process.env.PREVIEW_ENABLED === "true") {
        const response = await fetch(new URL("/_test/slack-preview-hints", backend), { signal: AbortSignal.any([abort.signal, AbortSignal.timeout(1000)]) });
        for (const raw of await response.json() as unknown[]) {
          const h = parseSlackHint(JSON.stringify(raw)); if (!h) continue;
          const key = `${h.bindingId}:${h.taskId}`;
          if (slackSeen.has(key) || slackActive.has(h.bindingId) || slackActive.size >= 4) continue;
          slackSeen.set(key, Date.now());
          const signal = AbortSignal.any([abort.signal, AbortSignal.timeout(120000)]);
          const work = composeSlack(process.env, signal).then(async ports => {
            try { await observeSlack(h, ports, signal); } finally { await ports.close(); }
          }).catch(() => {}).finally(() => slackActive.delete(h.bindingId));
          slackActive.set(h.bindingId, work);
        }
        for (const [key, at] of slackSeen) if (Date.now() - at > 300000) slackSeen.delete(key);
      }
      for (let i = 0; i < pending.length && active.size < 4; i++) {
        const hint = pending[i]; if (active.has(hint.contextId)) continue;
        pending.splice(i--, 1);
        const work = compose(process.env).then(ports => observe(hint, ports, abort.signal)).catch(() => {}).finally(() => active.delete(hint.contextId));
        active.set(hint.contextId, work);
      }
    } catch { /* Controller may still be starting. */ }
    await sleep(50, undefined, { signal: abort.signal }).catch(() => {});
  }
} finally { abort.abort(); await Promise.allSettled([...active.values(), ...slackActive.values()]); await fake.close(); }
