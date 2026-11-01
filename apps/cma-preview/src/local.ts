// Local queue/broker composition, launched and reaped only by scripts/local.py.
import { setTimeout as sleep } from "node:timers/promises";
import { fakeAppSync } from "../../preview/testing/fake.js";
import { compose } from "./composition.js";
import { observe, parseHint, type Hint } from "./observer.js";
if (process.env.APP_ENV !== "e2e") throw new Error("Local preview fixture only");
const backend = new URL(process.env.LOCAL_BACKEND_URL!);
if (backend.hostname !== "127.0.0.1") throw new Error("Loopback fixture only");
const abort = new AbortController();
const fake = await fakeAppSync(Number(process.env.LOCAL_PREVIEW_PORT));
for (const event of ["SIGTERM", "SIGINT"] as const) process.once(event, () => abort.abort());
const active = new Map<string, Promise<void>>(), pending: Hint[] = [], seen = new Map<string, number>();
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
      for (let i = 0; i < pending.length && active.size < 4; i++) {
        const hint = pending[i]; if (active.has(hint.contextId)) continue;
        pending.splice(i--, 1);
        const work = compose(process.env).then(ports => observe(hint, ports, abort.signal)).catch(() => {}).finally(() => active.delete(hint.contextId));
        active.set(hint.contextId, work);
      }
    } catch { /* Controller may still be starting. */ }
    await sleep(50, undefined, { signal: abort.signal }).catch(() => {});
  }
} finally { abort.abort(); await Promise.allSettled(active.values()); await fake.close(); }
