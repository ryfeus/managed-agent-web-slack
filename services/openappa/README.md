# Embedded OpenAPPA helper

Python sends version 3 JSON over stdin, with policy TOML, root/event/agent identity,
canonical tool, object arguments, and runtime. Unknown versions/fields are rejected. Stdout
contains a versioned typed decision or operational error code. No payload enters
command-line flags or diagnostic logs. The pinned upstream crates and transitive
versions are recorded in Cargo.lock.
The upstream copyright and license accompany the Lambda package in
`backend/src/managed_agents_app/policy/OPENAPPA-LICENSE.md`.

`../../scripts/build_openappa.sh --test` builds/checks a the pinned Rust 1.99.0 host helper.
The Lambda BuildKit workflow uses a digest-resolved Rust builder and locked
release tests to produce a static Linux x86-64 executable. Native test policy is
an isolated fixture, allowing sandbox policy deployments to differ from baseline.

Each evaluation opens fresh `Backend::Memory`, runs SessionStart and ToolCall, then
discards the native state. There are no operation receipts or disk trajectories.
A unique temporary TOML file is removed after `Config::load_from` reads it.
Upstream OpenAPPA owns policy interpretation. Exact tool contracts allow the governed built-ins. A
wildcard uses a declarative annotator that emits only the reserved ungrantable
`blocked` requirement. `--annotate-blocked` retains its version 1 annotation wire contract,
not an independent authorization engine. The sentinel executable path is resolved
for the host/Lambda package before opening the native runtime.

This hook admits pre-dispatch calls. OpenAPPA's `provider_run_tools` setting applies
to inference-response admission, which this embedding does not support, and stays
empty. CMA still executes tools after confirmation and delivers results directly.
Tool deltas/effects, history requirements, result confinement, context control and
sanitizers are unsupported. Annotators must explicitly declare empty `ranks`,
`audiences` and `effects` mandates; attention marks (including `blocked`) are supported.
New calls use the current policy; previously committed DSQL decisions are not reevaluated.
One native HITL authority/offer can return structured review text and a SHA-256 fingerprint binding call identity/arguments, trusted policy and authority. Resolve in a new process by reconstructing that offer, submitting the human ruling through the native host control hook, and executing the embedded remedy. Allow requires native `Authorized` for the exact call; deny requires native `Declined`. Non-HITL/multiple remedies fail closed. The review fixture is `tests/human-review.toml`; baseline remains automatic.

`tests/email-recipient-review.toml` is an illustrative MCP email fixture: internal recipients are allowed automatically and external recipients require human review. It is not deployed or wired to an agent or MCP server.

See [architecture](../../docs/architecture.md) and [limitations](../../docs/known-limitations.md).
