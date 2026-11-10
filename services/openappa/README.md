# Embedded OpenAPPA helper

Python sends version 6 JSON over stdin, with policy TOML, root/event/agent identity,
canonical tool, object arguments, and runtime. Unknown versions/fields are rejected. Stdout
contains a versioned typed decision or operational error code. No payload enters
command-line flags or diagnostic logs. The upstream revision is pinned in `scripts/prepare_openappa.py`; transitive versions are locked in Cargo.lock. Both local and Linux builds materialize that source and apply `patches/offer-kind.patch`, a one-line visibility change exposing existing read-only native offer classification. No engine algorithm is patched.
The upstream copyright and license accompany the Lambda package in
`backend/src/managed_agents_app/policy/OPENAPPA-LICENSE.md`.

`../../scripts/build_openappa.sh --test` builds/checks the pinned Rust 1.99.0 host helper.
The Lambda BuildKit workflow uses a digest-resolved Rust builder and locked
release tests to produce a static Linux x86-64 executable. Native test policy is
an isolated fixture, allowing sandbox policy deployments to differ from baseline.

Application evaluations open native `Backend::Sqlite` in a fresh temporary directory.
`SessionStart` runs once per context; subsequent processes reopen the same trajectory.
Native operation receipts retain call decisions, HITL offer IDs, resolutions and
result admissions. The Python snapshot port restores/uploads consistent standalone
SQLite files; DSQL atomically publishes their pointer with application decisions.
The optional memory path remains only for isolated native compatibility tests.
A unique temporary TOML file is removed after `Config::load_from` reads it.
Upstream OpenAPPA owns policy interpretation. Exact tool contracts allow the governed built-ins. A
wildcard uses a declarative annotator that emits only the reserved ungrantable
`blocked` requirement. `--annotate-blocked` retains its version 1 annotation wire contract,
not an independent authorization engine. The sentinel executable path is resolved
for the host/Lambda package before opening the native runtime.

This hook admits pre-dispatch calls and application-owned custom results. OpenAPPA's `provider_run_tools` setting applies
to inference-response admission, which this embedding does not support, and stays
empty. CMA still executes built-in/MCP tools after confirmation and delivers their results directly.
Result confinement may name only the trusted custom-tool inventory supplied by composition.
Label deltas, effect declarations and effect-history requirements are supported only for explicitly registered custom tools using SQLite. One HITL authority may waive native exclusion gaps; missing prerequisites cannot be waived. Context control remains unsupported. Annotators must explicitly declare empty `ranks`,
`audiences` and `effects` mandates; attention marks (including `blocked`) are supported.
A context pins the policy digest at its first checkpoint. Policy changes hold existing contexts; start a new conversation after an intentional policy upgrade. Committed decisions are never reevaluated.
One native HITL authority/offer can return structured review text and a SHA-256 fingerprint binding call identity/arguments, trusted policy, authority and persisted native offer. Resolve in a new process by reopening that stored offer, submitting the human ruling through the native host control hook, and executing the embedded remedy. Allow requires native `Authorized` for the exact call; deny requires native `Declined`. Non-HITL/multiple human offers fail closed. The review fixture is `tests/human-review.toml`; the application baseline now includes synthetic demos while existing web/metrics calls remain automatic.

`tests/email-recipient-review.toml` is an illustrative MCP email fixture: internal recipients are allowed automatically and external recipients require human review. It is not deployed or wired to an agent or MCP server.

See [architecture](../../docs/architecture.md) and [limitations](../../docs/known-limitations.md).

Custom calls use `host/cma-custom/<name>`. An allow returns a fingerprint; Python
retains it with the exact policy and optional human ruling in a private temporary
object. A result request carries this authorization plus `{text, is_error}`. The
fresh runtime checks the fingerprint and committed native authorization receipt,
reopens the original dispatch, and invokes native `ToolResult` for the exact call ID.
It reports `deliver` with exact admitted text, including native transformed `DeliverValue`, or `withhold` for a native replacement notice. Output never enters diagnostics. The decoded result limit remains 64 KiB; the 512 KiB JSON envelope allows escaped text. Python stores admitted output separately from the immutable raw outcome before fenced publication. Result recovery does
not execute a handler or change a committed decision. Unsupported/stale authorization
is an operational hold. Failure results permit only `The custom tool failed.` because
the pinned native failure hook does not inspect arbitrary error text.

Only deterministic native `redact-email` and `redact-secrets` output sanitizers are supported. The wrapper selects one unambiguous native sanitizer offer, executes its control remedy and re-proposes the unchanged exact call before execution. It never automatically selects raw Accept or Withhold. Native sanitizer failures/unsupported output remain held. Input/external/LLM sanitizers, general content scanning and result-release consent remain unsupported. `tests/custom-result-withheld.toml`
uses the native byte cap to prove withholding; it is a local test fixture only.

`tests/stateful-custom.toml` proves a reviewed custom read narrows the accumulated
audience to HR and a later fresh helper denies a public recipient while allowing
HR. The built-in [application demos](../../docs/policy-demos.md) additionally exercise native trust, result redaction and effect-history remedies. See the
[rollout and recovery instructions](../../docs/operations.md#sqlites3-persistence-rollout).
