## Summary

## Validation

- [ ] Ran `./scripts/verify --fast`.
- [ ] Ran `./scripts/verify --full`, or explained why it is not applicable.
- [ ] Added deterministic tests for behavioral changes.

## Architecture and release checks

- [ ] Preserved the Anthropic transcript/execution and DSQL metadata-only boundary.
- [ ] Updated persistence and feature contracts where required.
- [ ] Did not add unapproved provider SDK imports or test-disablement exceptions.
- [ ] Did not add secrets, deployment identifiers, generated files, or local configuration.
- [ ] Updated relevant documentation and configuration examples.
