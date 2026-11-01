# Testing

| Command | Evidence |
| --- | --- |
| `npm run check` | Pinned native Rust build/tests, local CMA lock validation, destructive-deploy guard, ESLint/Ruff/format, TypeScript/mypy, Python unit/contracts and E2E helper tests; no Docker/cloud/browser |
| `npm run e2e` | Isolated PostgreSQL migrations/transactions, official A2A SDK interoperability and push, controller recovery, signed Slack ingress, real AG-UI HTTP and semantic Playwright |
| `npm run e2e:production` | Static-exported Web authentication/history/Stop smoke |
| `npm run build` | Web export plus minimal-context, cached Lambda packages and artifact validation |
| `npm run terraform:validate` | Both roots' validation and mocked deployment-resource tests |
| `npm run secrets:check` | Pinned Gitleaks 8.24.3, redacted scan of tracked and nonignored untracked files |
| `npm run verify:full` | Check, production Web, Lambda artifacts, Terraform and full local E2E |

Install dependencies/browser explicitly as described in [local development](local-development.md). CI runs these stages separately and retains failing browser artifacts. The standard scanner replaces private identifier/credential regexes; install the exact Gitleaks release (CI verifies its release archive checksum). Public export includes only tracked distributable files and excludes plans, dotenv/runtime/generated material and symlinks.

## Human-input evidence

Rust tests and `test_openappa_policy.py` use the pinned native engine and a real `requires.attention` + HITL policy. They prove actual review, reconstructed native allow/deny, stale consent rejection and unsupported remedies. PostgreSQL controller tests prove first-writer wins, active Task/lease/cancellation fences, commit-before-confirm and lost-confirmation recovery. Signed Slack integration and `human-review.spec.ts` prove ownership, exact call/explanation, Web reload, either surface resolving the review, duplicate/late rejection and cross-surface control retirement.

Local execution uses fake CMA and a Slack API recorder. It proves application semantics and native policy, not Anthropic's deployed approval contract, installed Slack scopes, native Slack button clicks or client rendering. Tests never replace native review with an application `ask => allow` shortcut. Provider SSE is forbidden on the local P0 path.

The intermittent Web clarification panel problem remains open despite enabled tests sometimes passing. See [known limitations](known-limitations.md); do not mark it fixed from an isolated pass.

## Optional live evidence

Only run against an explicitly configured/authorized sandbox:

```bash
RUN_LIVE_E2E=1 npm run test:live -- all
```

The independent preflight loads `.env`, checks STS (and `EXPECTED_AWS_ACCOUNT_ID` when set), exports concrete credentials, discovers Terraform outputs/resources, verifies deployed configuration, both Slack tokens/scopes/channel access, Web authentication, and matching DSQL principals. No writes occur before those checks; it never authenticates interactively or provisions resources. Missing access is reported with secret-safe diagnostics.

`smoke` posts genuine mention/reply messages with the local user token. `approval` uses genuine delivery to check automatic OpenAPPA policy; `all` adds clarification/Stop via authenticated Web. These suites do **not** install the HITL fixture. Signed interaction fixtures prove backend handling, not native client interaction. Never ask a user to open Slack, log in, send prompts, click controls or inspect its UI for testing.

`e2e/live/human-review.spec.ts` is a separate opt-in acceptance suite: genuine Slack prompts, matching Web/Slack tool arguments and explanation, Web reload/Allow, signed Slack Deny/duplicates, Stop, canonical CMA confirmations and control retirement. First authorize a sandbox policy rollout, require no active Tasks, and pause scheduler consumption. Deploy the existing `services/openappa/tests/human-review.toml` fixture as the scheduler's packaged policy, then resume consumption. Run the normal live preflight, set `E2E_LIVE_HUMAN_REVIEW_POLICY=1`, and invoke this file through `playwright.live.config.ts`. Always settle/cancel test Tasks and restore the original scheduler artifact/policy under quiescence, verify its code hash, and resume consumption. Do not change the committed default policy or claim native Slack clicks from signed fixtures. Live browser artifacts use `test-results/live-playwright` separately from local suites.

For other live Playwright cases, run the same preflight from `scripts/live.py` before invoking `playwright.live.config.ts`; preserve its opt-in environment gate. Direct DSQL/Anthropic contract tests in `backend/tests/integration/test_real_services.py` require `RUN_INTEGRATION_TESTS=1` and are never included in local/CI checks.

Historical Phase 3 acceptance on 2026-10-31 proved genuine Slack delivery, stateless automatic allow/deny, clarification and Stop. It did not prove the newly added human-review remedy.

Authorized Phase 4 sandbox acceptance on 2026-10-31 passed genuine mention/reply delivery, exact-message receipts/source links, automatic `web_fetch`, clarification/Stop, and all three native human-review cases. Web Allow survived reload and produced one CMA allow plus one successful result; signed Slack Deny/duplicates produced one deny plus an error result; pending-review Stop produced one interrupt, no allow and an interrupted result. Web/Slack arguments matched, controls retired across surfaces, and completed turns had one visible answer. Direct DSQL/CMA checks also verified the deployed session's exact version/environment. The original policy/artifact was restored and its hash checked after testing.

Live verification exposed and fixed DSQL's unsupported inline constraint on added columns, the scheduler's missing `s3:PutObject` permission on the existing private pending prefix, and Slack's automatic URL rewriting in tool arguments (now `verbatim`). Migration preserved all 213 prior Tasks and 76 prior requests. Native Slack-client interaction remains unverified.

The Phase 4 Slack manifest was subsequently applied through the configuration API and verified by export: seven required bot scopes, five event types, no unfurls, and Socket Mode disabled. Existing organization readiness, user OAuth and MCP settings were preserved. Slack returned `permissions_updated: true`; the existing bot token still has `im:read`, `im:write`, `links:read`, `reactions:read` and `users:read`. Removing these existing grants requires a separate credential change under [Slack's token lifecycle rules](https://docs.slack.dev/app-management/distribution/).

After the manifest update, genuine mention/reply smoke and all three live automatic-policy/clarification/Stop tests passed with the existing token. This confirms delivery and runtime behavior; a newly issued token limited to the seven configured scopes has not been tested.

Final health checks matched all ten Lambda code hashes to the built artifacts, found no active Tasks, and observed all working queues drain. No new dead letters appeared; the 18 historical push dead letters were retained. The only post-deployment Terraform difference was AWS expanding the private API policy's shorthand resource into its equivalent ARN.
