# Testing

| Command | Evidence |
| --- | --- |
| `npm run check` | Pinned native Rust build/tests, local CMA lock validation, destructive-deploy guard, ESLint/Ruff/format, TypeScript/mypy, Python unit/contracts, E2E helpers and TypeScript gateway/observer/AppSync protocol tests; no Docker/cloud/browser |
| `npm run e2e` | Isolated PostgreSQL migrations/transactions, official A2A SDK interoperability and push, controller recovery, signed Slack ingress, real AG-UI HTTP and semantic Playwright |
| `npm run e2e:production` | Static-exported Web and bundled Node gateway authentication/history/navigation smoke |
| `npm run build` | Web export plus minimal-context, cached Lambda packages and artifact validation |
| `npm run terraform:validate` | Official offline SAM transform/CloudFormation schema checks, both Terraform roots, and mocked resource/network/IAM/rollback tests |
| `npm run secrets:check` | Pinned Gitleaks 8.24.3, redacted scan of tracked and nonignored untracked files |
| `npm run verify:full` | Check, production Web, Lambda artifacts, Terraform and full local E2E |

Install dependencies/browser explicitly as described in [local development](local-development.md). CI runs these stages separately and retains failing browser artifacts. Gitleaks checks credentials; the retained publication check in `npm run check` separately rejects known private deployment identifiers. Install the exact Gitleaks release (CI verifies its release archive checksum). Public export includes only tracked distributable files and excludes plans, dotenv/runtime/generated material and symlinks.

See [Phase 5 implementation and rollout evidence](phase-5-rollout.md) for baseline and final local results. Preview tests use real local HTTP SSE and WebSocket frames but fake CMA/AppSync services; no local command activates previews in AWS.

## Slack preview evidence

Phase 6 adds worker tests for title escaping/Unicode, offsets, duplicate/gapped frames, throttling, the forty-call budget, deadline cleanup, stale hints and Slack errors. Cross-language PostgreSQL tests contend a TypeScript preview claim with the Python projector, expire the preview lease and prove terminal/input-required fencing and unchanged P0 receipts. `e2e/slack-preview.spec.ts` uses the existing loopback AppSync WebSocket adapter and `RecordingSlack`: one CMA reader feeds Web plus Slack, drafts never enter canonical Markdown, outages/ambiguous appends preserve the final answer, and native human review closes the draft card. The recorder models replacement by task ID, preserving source links.

The existing local preview fixture also dispatches Slack hints; there is no extra supervisor or live credential. The isolated stack sets both environment switches, while each test opts in with `/_test/slack-preview-control`; reset disables that trigger. `/_test/preview-control` separately controls the existing producer. These routes exist only in local mode. `npm run e2e -- --grep 'Slack preview|Slack and Web share|human input closes'` runs focused acceptance after PostgreSQL checks. Production defaults remain disabled. Local replacement assertions do not establish native Slack-client rendering; see [Phase 6 rollout](phase-6-rollout.md).

## Human-input evidence

The illustrative native fixture [`email-recipient-review.toml`](../services/openappa/tests/email-recipient-review.toml) tests internal allow, external review, schema rejection, exact-recipient consent and the comma-joined-address limitation; it is not deployed.

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

Historical sandbox acceptance on 2026-10-31 verified genuine Slack delivery,
stateless automatic policy, clarification, Stop and native human review. Web Allow
survived reload; signed Slack Deny/duplicates and pending-review Stop produced
one corresponding provider action. Controls retired across surfaces and canonical
answers remained visible. These historical results are distinct from public-branch
verification and from native Slack-client acceptance.

The updated manifest was validated through the Slack configuration API with seven
required bot scopes, five events, no unfurls and Socket Mode disabled. Existing
organization/user OAuth settings were retained. Tests used an existing token with
additional historical grants; a fresh token limited to the seven scopes remains
unverified. Manifest updates alone do not revoke existing token grants.

The TypeScript gateway subsequently passed Web/Slack continuity and native review
acceptance, including two live preview subscribers sharing one observer. Other
turns completed without previews. The original policy was restored after testing
and previews were disabled. See [Web rollout evidence](phase-5-rollout.md) and
[Slack preview limits](phase-6-rollout.md). No live acceptance runs automatically
in local verification or CI.
