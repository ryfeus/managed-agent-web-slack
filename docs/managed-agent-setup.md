# Managed Agent setup

The application reuses one Anthropic Managed Agent and one Anthropic-managed
environment for its conversations. They are deployment configuration, not source
defaults.

Set the following values in an untracked `.env` before a real deployment:

```text
CLAUDE_AGENT_ID=agent_...
CLAUDE_ENVIRONMENT_ID=env_...
ANTHROPIC_API_KEY=...
```

`AGENT_ID` remains a deprecated alias for `CLAUDE_AGENT_ID`. A deployed function
fails clearly before Managed Agent use when either canonical identifier is absent.
The deterministic local E2E runtime injects synthetic IDs and does not require
Anthropic credentials.

## Webhook

After deployment:

1. Read `anthropic_webhook_url` from `terraform -chdir=infra/app output`.
2. In Claude Console, create a webhook for `session.status_idled`,
   `session.status_terminated`, and `session.budget_reached`.
3. Store the one-time signing key in `.env` as `ANTHROPIC_WEBHOOK_SIGNING_KEY`.
4. Run `npm run secrets:sync` with concrete AWS credentials.

The ingress verifies the raw payload with the official SDK before publishing a
small session-change notification. The Slack projector retrieves canonical events;
it never treats an idle notification itself as an agent answer.

## Transcript invariant

Session events are replayed directly from Anthropic. The same pure reducer handles
replayed and live events, and the browser does not send old message history with a
new turn.
