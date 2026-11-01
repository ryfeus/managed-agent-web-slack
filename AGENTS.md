# Repository agent context

## Invariants

Web and Slack use authorized application threads and A2A Tasks. Only the private CMA controller uses Anthropic session/event APIs. CMA owns canonical transcripts and execution; DSQL holds identity, routing, idempotency, leases, projection state and opaque temporary-object keys, never transcripts, message bodies, tool arguments/output or reasoning.

Read relevant implementation/tests before edits. Preserve Runtime/port composition; handlers do not construct provider clients. The active Task is the earliest nonterminal FIFO Task. Load canonical history before subscriptions and reconcile after closure; disconnect never cancels. Explicit Stop remains independent of policy.

Native OpenAPPA evaluates each approval in fresh memory. A single supported HITL offer may expose exact call/policy review to authorized Web/Slack surfaces. First authorized ruling wins; the scheduler reconstructs the native remedy, checks call/policy/authority fingerprint, and commits a fenced final decision before CMA confirmation. Changed consent rotates the request ID. Preserve uncertain-confirmation markers and fail closed for unsupported remedies/outages. No direct surface approval bypass.

## Workflow

- `npm ci`, `uv sync --directory backend --locked`, and `npx playwright install chromium` are explicit setup.
- `npm run dev` is foreground, credential-free PostgreSQL/fake-CMA/Slack-recorder development. Use printed URLs; Ctrl-C cleans up only its children/Compose project. Never read `.env` or inject live credentials into local runs.
- Run `npm run check` during edits and `npm run verify:full` before completion. `npm run e2e -- --grep …` focuses Playwright after PostgreSQL checks. Run pinned `npm run secrets:check` as well.
- Test behavior, including duplicates, failures and races when relevant. Preserve focused trust-boundary tests. Record material unresolved gaps in `docs/known-limitations.md`. Do not claim live compatibility from local fakes or silently skip failures.
- Keep docs current and concise. No mandatory feature-contract template, JSON registry or custom checker framework. For knowledge edits keep immediate indexes and semantic log current.

## AWS and deployment

Default region is `us-west-2`; preferred local SSO profile is `default` via `AWS_PROFILE`. Discover account with STS; `EXPECTED_AWS_ACCOUNT_ID` is an optional assertion. Use the AWS account/credentials/remote-state skills when applicable. Verify `aws sts get-caller-identity --profile "$AWS_PROFILE"` before apply and export concrete SSO credentials before direct Terraform/Python AWS use.

Use `npm run cma:plan`, `npm run deploy:plan`, then explicit `npm run deploy`. Terraform outputs are authoritative; backend configuration lives under ignored `.generated/terraform/`; `TF_STATE_BUCKET` selects existing state. Preserve destructive-change review, DSQL deletion protection, versioned S3 Lambda artifacts and least privilege. Never automatically deploy or destroy resources during local work.

Only `cma/claude-lock.json` selects agent ID, exact version and environment ID. Serialize CMA applies, use ant >=1.30.0, retain/review partial lock updates and never force/prune automatically. Environment selection is ID-based, not version-pinned. `.env` secrets are local: never print, commit, copy into docs or Terraform state. Webhook signing keys sync only after registration.

## Slack trust and operations

Keep Socket Mode disabled. Render/import the manifest and reinstall after scope changes; manifest text alone does not prove installed permissions. HMAC authenticates raw ingress; verified team/human identity must map through DSQL and own the thread. Optional allowlists are not credentials. Plain channel messages are accepted only in an owned bound thread. Exact local user-token bot/app metadata exceptions must match both configured IDs.

One baseline supports mentions/DMs, bound replies/linking, processing/reactions, chunk task cards with normal final-reply fallback, source links when `PUBLIC_APP_URL` is set, Stop and native review Allow/Deny. Clarification may be answered through Web. Never mix Slack text-mode and chunk-mode payloads. Working-state failures must not suppress/resubmit execution. Never project thinking.

Slack testing is API-only. Never ask a user to open Slack, log in, post prompts, click controls or inspect its UI. `RUN_LIVE_E2E=1 npm run test:live` performs read-only AWS/DSQL/deployment/token/identity preflight before genuine user-token delivery. The user token stays local and never enters Secrets Manager/Lambda. Report missing access with secret-safe diagnostics and distinguish signed fixtures from native UI acceptance. Local verification never triggers live tests automatically.

Inspect Slack boundaries in ingress → agent-input → webhook → projector order, including controller/scheduler/push when needed. Preserve run/Task metadata, never payload bodies. See `docs/architecture.md`, `docs/testing.md`, `docs/operations.md`, and `docs/slack-setup.md`.
