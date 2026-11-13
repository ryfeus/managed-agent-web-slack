# Testing

`npm run check` also builds and runs the [standalone support-triage tests](../examples/support-triage/README.md#tests). They exercise real pinned OpenAPPA in fresh helper subprocesses with temporary SQLite: exact secret masking, history-dependent review, repeated Allow/Deny, invalid inputs, withheld output, unsupported remedies and missing state. Business tools and CMA delivery are mocked; this does not establish live-provider acceptance.

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

## Stateful policy demos and admitted output

`test_demo_policy.py` runs real native hooks with fresh helpers and restored SQLite for exact email/secret transformations, fixed failures, output bounds, ambiguous/unsupported sanitizer rejection, accumulated audience/trust, missing prerequisites, completed/reserved effects and review Allow/Deny across reopening. `test_admitted_results_postgres.py` verifies separate raw/admitted artifacts, atomic pointer publication, storage/commit failures, lost delivery acknowledgments, missing committed output and no handler replay/raw fallback. Cancellation, lease, Stop, clarification and built-in/MCP review regressions remain in the full suites.

`e2e/policy-demos.spec.ts` checks prompt-only starters, Web review/reload, stateful denials and signed Slack publication/repeat denial. Linux release packaging runs sanitizer, history/review and accumulated-state smoke checks on the actual Amazon Linux base. [Demo prompts and semantics](policy-demos.md) explain the native trust guard. Run `npm run verify:full` and pinned `npm run secrets:check` on the public tree; originating-repository results do not replace those checks.

The opt-in `RUN_LIVE_E2E=1 npm run test:live -- demos` checks synthetic redaction, exact admitted publication arguments, Web approval/reload, repeat-effect Allow/Deny, persisted private-state public denial and permitted internal publication. Prompts use genuine Slack delivery; internal publication uses a signed Slack Allow fixture sent twice. It is separate from `all` and requires the coordinated demo deployment. Its inspector emits comparison booleans, not report text; signed fixtures do not prove native Slack-client clicks or rendering.

Historical acceptance in the originating sandbox on 2026-11-08 covered migration 015, protocol 6 and CMA agent version 4. Both demo cases passed: canonical evidence confirmed exact redacted output and publication arguments, no execution after Deny, successful publication after an authorized effect waiver, and public denial after a private read across turns. Duplicate signed Slack Allow produced one internal execution and retired Web controls. Native Deny persists for identical rendered arguments; the test uses different synthetic text for a later review. The `all` suite also passed custom tools, built-in approval, clarification and Stop. Snapshot integrity, policy binding, admitted-output digests and scheduler-only snapshot access were verified separately. These are historical sandbox results, not acceptance of this public checkout. Storage failures, uncertain delivery and withholding retain local fault-injection evidence only.

## SQLite/S3 persistence

`test_policy_snapshots.py` exercises real native SQLite across fresh helper processes: persisted approval, accumulated audience, incompatible denial/permitted continuation, receipt replay, changed result rejection, missing/corrupt objects, policy pinning and WAL-safe backup. The S3 adapter test checks immutable encrypted writes, bounded reads and stream closure.

`test_policy_checkpoints_postgres.py` checks atomic pointer/decision publication, rollback under stale revision/lease/cancellation, lost acknowledgments at both boundaries, result-before-next-call ordering, missing state after execution and orphan retention. The existing Web/signed-Slack custom-tool and review suites use the persistent composition. Focus with `npm run e2e -- --grep 'custom|approval|review|Stop|clarification'`, then run the full verification commands.

Historical sandbox acceptance on 2026-11-08 covered migration 014 and the protocol 5/IAM update with new conversations and the automatic baseline. The live `all` suite passed custom tools, built-in approval, clarification and Stop. The shared custom-tool snapshot reopened as standalone SQLite with valid checksums, policy binding and native receipts; IAM checks permitted snapshot access only for the scheduler among the tested roles. Stateful label/HITL, withholding, lost acknowledgments and storage failures were covered locally at that checkpoint; subsequent demo acceptance is described above. Current upgrades use protocol 6. No Lambda latency benchmark was performed.

## Slack preview evidence

Phase 6 adds worker tests for title escaping/Unicode, offsets, duplicate/gapped frames, throttling, the forty-call budget, deadline cleanup, stale hints and Slack errors. Cross-language PostgreSQL tests contend a TypeScript preview claim with the Python projector, expire the preview lease and prove terminal/input-required fencing and unchanged P0 receipts. `e2e/slack-preview.spec.ts` uses the existing loopback AppSync WebSocket adapter and `RecordingSlack`: one CMA reader feeds Web plus Slack, drafts never enter canonical Markdown, outages/ambiguous appends preserve the final answer, and native human review closes the draft card. The recorder models replacement by task ID, preserving source links.

The existing local preview fixture also dispatches Slack hints; there is no extra supervisor or live credential. The isolated stack sets both environment switches, while each test opts in with `/_test/slack-preview-control`; reset disables that trigger. `/_test/preview-control` separately controls the existing producer. These routes exist only in local mode. `npm run e2e -- --grep 'Slack preview|Slack and Web share|human input closes'` runs focused acceptance after PostgreSQL checks. Production defaults remain disabled. Local replacement assertions do not establish native Slack-client rendering; see [Phase 6 rollout](phase-6-rollout.md).

## Human-input evidence

Application custom tools have native call/result correlation, result-cap withholding, namespace isolation and reconstructed HITL tests. PostgreSQL tests cover commit-before-execute/deliver, duplicate wakeups, lost result-object acknowledgments, policy-only recovery, uncertain execution/delivery, cancellation, lease expiry and mixed pending calls. `e2e/custom-tools.spec.ts` covers Web execution, signed Slack Allow/Deny with Web reload, withheld-output privacy and Stop. `npm run e2e -- --grep 'custom tool'` runs focused browser acceptance after the PostgreSQL checks. The local `custom-result-withheld` fixture lowers the native result cap; it uses the real pinned engine and never replaces policy with a fake allow/deny switch.


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

`smoke` posts genuine mention/reply messages with the local user token. `approval` uses genuine delivery to check automatic OpenAPPA policy; `all` adds clarification/Stop and `text_metrics` through Slack and authenticated Web. The custom-tool case checks one execution/result per invocation, exact Unicode metrics against canonical CMA history, native authorization metadata and Web reload. Its inspector emits metadata and comparison booleans, never arguments or output bodies. These suites do **not** install the HITL fixture. Signed interaction fixtures prove backend handling, not native client interaction. Never ask a user to open Slack, log in, send prompts, click controls or inspect its UI for testing.

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

Historical custom-tool sandbox acceptance on 2026-11-07 covered migration 013 and
CMA agent version 3 under quiescence. Slack and Web each invoked `text_metrics`
once and returned exact native-admitted Unicode metrics through canonical
`user.custom_tool_result`, with resolved execution/delivery records. Built-in
approval, clarification and Stop also passed. Provider-capacity failure exposed
the [retry-exhaustion status gap](known-limitations.md); uncertainty and withholding
remain locally tested failure paths, not live fault-injection claims.
