# Local development

Install Node 24, uv/Python 3.14, Docker with Compose, and Rust 1.99.0 (including rustfmt). The pinned dependencies are explicit setup steps:

```bash
npm ci
uv sync --directory backend --locked
npx playwright install chromium
npm run dev
```

The foreground runner prints the actual Web/API URLs and `test-results/managed-local-*/` log directory. Sign in with `e2e-access-token`. Preferred ports are Web 3000, Node API 3001, private Python controller 3002, preview fixture 3003 and PostgreSQL 54321; occupied ports get alternatives. Ctrl-C stops only owned child process groups and the unique Compose project, deleting its disposable database. Failure logs remain. There are no detached sessions, process registries or automatic dependency installations. Concurrent checkouts use separate databases; avoid simultaneous Next builds in the same checkout.

Local mode always selects fake CMA, local events, recorded Slack and loopback PostgreSQL. The runner copies a small environment allowlist, sets fake application credentials, disables EC2 metadata, and passes an empty Compose env file. It never reads root `.env`. Any `apps/web/.env*` file other than `.env.example` blocks startup because Next would load it implicitly. Local test endpoints are unavailable outside the fake runtime.

## Check and focus

```bash
npm run check
npm run e2e -- --grep 'native OpenAPPA review'
npm run e2e:production
```

Normal Playwright arguments pass through unchanged. `e2e` first runs real PostgreSQL transaction/migration/integration tests, the TypeScript/Python shared-repository PostgreSQL tests, then the semantic browser suite against fresh fake services. Traces/screenshots and service logs remain under ignored test folders. Chromium is installed explicitly, never by the runner.

Use `/_test/state`, `/_test/a2a/script` and `/_test/events/drain` for local fixtures. To exercise real native HITL, POST `{"fixture":"human-review"}` to `/_test/policy`, then script a `web_search` call with `requires_action`. The complete reproducible example is [human-review.spec.ts](../e2e/human-review.spec.ts); it covers reload, Web allow/deny and signed Slack settlement. `{"fixture":"baseline"}` restores normal automatic decisions; `/_test/reset` also restores baseline. These fixtures use the real pinned OpenAPPA binary with fake provider execution.

## Explicit live testing

`RUN_LIVE_E2E=1 npm run test:live -- all` loads `.env` deliberately and runs independent read-only account/deployment/token/identity preflight before API-only sandbox tests. `smoke` selects genuine Slack mention/reply delivery; `approval` selects the automatic policy check. It does not deploy, log in, remap identities or apply definitions. See [testing](testing.md) for coverage limits and [Slack setup](slack-setup.md) for prerequisites.

The same runner starts a loopback AppSync WebSocket/HTTP fixture and independent Node preview observer. A local `/_test/preview-control` request with `{"enabled":true}` enables scheduler hints for a test; reset disables them. `/_test/preview/config` injects publish outages, dropped delivery, duplicates or subscribe rejection. `e2e/preview.spec.ts` emits real CMA delta shapes after an observer connects, verifies two-viewer fanout and exact canonical reconciliation, and counts one observer/execution. Local-only fixture endpoints and fake authentication are never enabled in production.

`npm run e2e:production` builds and executes the Web Function bundle as well as the exported frontend. `npm run test:functions` runs focused gateway/observer/AppSync unit and loopback protocol tests. Terraform validation uses pinned official SAM translator and cfn-lint from `scripts/requirements-iac.txt`; it does not load cloud state or credentials.
