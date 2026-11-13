# Support triage with custom tools

A runnable version of the November 13 **Own the Tool, Govern the Result** scenario:
read a customer ticket, mask service logs before the simulated model sees them,
and distinguish internal issue creation from public issue creation.

The business tools and CMA delivery are mocked. Policy decisions, sanitizer
execution, maintainer remedies and SQLite history use real OpenAPPA, pinned to
`c7c1e1ef36aa07604b421c48be266d7397557656`, with the app's read-only offer-inspection
patch. Nothing is sent to a tracker, CMA, AWS, Web or Slack.

## Run

From the repository root, with Python 3, Node 24/npm and Rust 1.99.0 available:

```sh
npm run example:support-triage
```

The wrapper also recognizes the repository's local `.generated/cargo` toolchain.
It prepares the revision-verified native source and builds using the example's
lockfile. Initial source/dependency downloads may need network access. Execution
requires no credentials, database service or `.env` file.

The default is interactive: enter `allow` or `deny` at each maintainer review.
The terminal shows the native review and exact call arguments. EOF stops the run;
it never grants approval. For an unattended demonstration:

```sh
npm run example:support-triage -- --review allow
npm run example:support-triage -- --review deny
```

These modes explicitly label their rulings as scripted, not human consent. Every
run creates a new private temporary trajectory and deletes it on normal completion
or a handled error. There is no resume command. Forced termination can leave OS
temporary files containing synthetic policy inputs/results.

## What happens

| Step | Custom call | Expected native outcome |
| --- | --- | --- |
| 1 | `create_issue(tracker="public", …)` | Allow while the audience is public |
| 2 | `read_ticket(ticket_id="T-4821")` | Accept narrowing the audience to internal |
| 3 | `fetch_logs(ticket_id="T-4821")` | Select `mask-secrets`; admit masked text at internal |
| 4 | `create_issue(tracker="internal", …)` | Allow, with admitted ticket/log content |
| 5 | Repeat step 1's exact public arguments | Require maintainer review |
| 6 | Repeat those arguments with another call ID | Require review again after Allow |

The log result includes:

```text
T-4821 service=checkout status=503
api_key=[redacted-secret]
retries=3
```

Each admitted output is displayed as a simulated `user.custom_tool_result`,
correlated to the originating `agent.custom_tool_use` ID. Issue receipts contain
`simulated: true`, the tracker and invocation ID; no issue URL is invented.
With scripted Allow, the summary reports six mock executions and six results.

The public issue body is deliberately harmless and identical in steps 1, 5 and 6.
Review depends on conversation history, not a classifier's judgment of the text.
Approval releases one call without restoring the public audience. With scripted
Deny, step 5 never executes; the native denial also applies to step 6's identical
arguments without another prompt. That run has four mock executions and six
results, including two fixed denial results.

## Wiring and boundaries

- [tools.yaml](tools.yaml): CMA custom-tool declaration fragment, not a deployed
  agent definition or a replacement for the application's lockfile.
- [tools.rs](tools.rs): strict mock inputs and deterministic handlers. Both reads
  accept only `T-4821`; issue trackers are `internal` or `public`. Extra fields and
  incorrectly typed inputs fail.
- [appa.toml](appa.toml): complete input contracts, audience deltas, a scoped
  `redact-secrets` binding and a single `maintainer` HITL authority.
- [main.rs](main.rs): simulated agent/controller. [native.rs](native.rs) translates
  call/result boundaries into native hooks. Each start, call, ruling and result
  uses a fresh helper process reopening the same SQLite trajectory.

The runner validates inputs, checks the call, resolves supported native remedies,
executes the mock, checks its result, then places only admitted text in the
simulated model-facing sink. The internal issue body uses that sink's ticket and
masked-log results. Raw logs travel only through private helper stdin and native
state; console output and result delivery never use a raw fallback.

The ticket read accepts only the native narrowing offer. Log reads select only
the unambiguous `mask-secrets` output remedy, never raw Accept. Unsupported offers,
unavailable masking, changed policy, missing/corrupt state and result withholding
stop the run. Native operation receipts retain decisions and exact admissions;
duplicate dispatch in the runner is rejected. The masker handles recognized
patterns, not arbitrary confidential information.

This example has its own Rust package using the existing pinned dependencies.
It does **not** extend the production CMA adapter: that adapter currently rejects
the standalone narrowing acceptance required by this ticket read. Copying this
policy into the app therefore does not enable a ticketing integration. The app's
agent, policy, UI and Lambda package are unchanged.

The example demonstrates policy memory and the custom-tool ownership boundary.
It does not implement the production scheduler's DSQL fences, S3 checkpoints,
recovery, authenticated review surfaces or external-write idempotency. Local
success is not live CMA or Slack acceptance. Composio/account selection remains
outside this example. The knowledge-base article and its deferred links are not
modified here.

## Tests

Focused tests start real helper subprocesses and cover the six-step run, repeated
masking, Allow/Deny, removal of the maintainer, strict inputs, withholding,
unsupported remedies, stale review, admission replay, policy pinning, missing
state, terminal EOF and temporary-state cleanup.

```sh
# Includes the example's build, format check and focused tests:
npm run check

# After pinned source preparation, run just the example tests:
bash scripts/support_triage.sh --test

# Repository completion checks:
npm run verify:full
npm run secrets:check
```

No local check deploys infrastructure or starts live-provider tests.
