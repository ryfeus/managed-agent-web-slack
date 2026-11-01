# Contributing

Start with [local development](docs/local-development.md) and [architecture](docs/architecture.md). Keep changes small enough to explain; delete obsolete branches and commands instead of adding compatibility wrappers.

Run `npm run check` while editing. Before completion run `npm run verify:full` and `npm run secrets:check`; report blocked stages honestly. Add focused behavior tests at the innermost useful layer, including duplicate/failure/race cases when those semantics change. Keep unresolved correctness gaps in [known limitations](docs/known-limitations.md), without disabling unrelated tests.

Preserve application-thread/A2A ownership and controller-private CMA identity. DSQL is metadata only. Keep signatures, ownership, private routing, IAM and destructive-deploy guards intact. Do not log message bodies, tool arguments, reasoning or secrets. Native human rulings must pass fresh OpenAPPA resolution and a fenced final decision before CMA confirmation.

Local checks use fake providers and never deploy. Live tests and deployment require explicit sandbox configuration/authorization. Slack testing is API-only; report missing credentials or unsupported native interactions without asking a user to operate Slack. Review [operations](docs/operations.md) before any real deployment. Commit source/lock changes, never `.env`, generated output, runtime artifacts or bytecode.
