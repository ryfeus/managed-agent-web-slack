# Contributing

## Local development

Install Node.js 22+, Python 3.14 with `uv`, Docker with Compose v2, and Terraform.
The provider-free workflow is:

```bash
npm ci
uv sync --directory backend --locked
npm run dev:e2e
```

Run `./scripts/verify --fast` while working and `./scripts/verify --full` before
submitting a change. Focused Python tests run with
`uv run --directory backend pytest <path>`; frontend tests run with `npm run test:web`.

## Architecture rules

Anthropic owns conversation transcripts and execution state. DSQL stores only
identity, authorization, ownership, routing, idempotency, and projection metadata.
Do not add transcript, message-body, tool-output, or reasoning fields to DSQL.

Provider SDK imports belong only in their approved adapters. Preserve the Runtime
and port boundaries; handlers must not construct provider clients. The fast
verifier enforces these constraints, the persistence schema contract, and test
disablement rules.

For distributed, protocol, persistence, authorization, or external-integration
changes, start from [the feature-contract template](docs/feature-contract-template.md).
Add tests at the innermost layer that proves the behavior, including deterministic
duplicate, retry, failure, race, and authorization coverage when applicable.

## Deployment and documentation

Never commit credentials, deployment endpoints, account IDs, generated manifests,
or generated Terraform backend configuration. Use `.env.example` for documented
configuration and `.generated/` for rendered deployment files. Update setup,
architecture, and operations documentation whenever behavior changes.

Live-provider tests are explicitly opted in and are not a substitute for local
deterministic coverage. Record material unresolved correctness gaps in
`knowledge/known-limitations.json` with their executable test reference.
