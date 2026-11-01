# Slack setup

Use signed HTTP Events API delivery with Socket Mode disabled. Bot token and signing secret belong in untracked `.env`/Secrets Manager; workspace/user/channel IDs are routing data, not credentials. App-level Socket Mode tokens are unused.

## Manifest and installed scopes

After deployment run `npm run slack:manifest`, import `.generated/slack-manifest.yaml`, and reinstall the app after scope/event changes. The renderer can also accept `--public-app-url https://example.cloudfront.net`. Terraform `slack_events_url` and `slack_interactions_url` are authoritative. Invite the bot to the selected channels.

For API administration, keep `SLACK_APP_CONFIGURATION_TOKEN` and `SLACK_REFRESH_TOKEN` in local `.env`; they are not deployed credentials. Export the existing manifest, validate the complete proposed configuration, apply it, and export again to verify. [Manifest updates replace the configuration](https://docs.slack.dev/reference/methods/apps.manifest.update/), so preserve existing user OAuth/MCP settings outside the bot change. An existing org-ready app must retain `org_deploy_enabled: true`; Slack rejects disabling it even though the fresh-app template defaults to false.

Inspect `permissions_updated` and the installed token's `x-oauth-scopes` separately. Removing scopes from the manifest does not remove grants from an existing token; [Slack retains those grants until token revocation or app uninstallation](https://docs.slack.dev/app-management/distribution/). Credential replacement requires its own installation/revocation and secret-sync procedure; a successful manifest update alone does not establish reduced token privileges.

| Scope | Purpose |
| --- | --- |
| `assistant:write` | Agent View status/lifecycle |
| `chat:write` | Replies, task cards and approval messages |
| `app_mentions:read` | Top-level channel prompts |
| `im:history` | DM/Agent View messages |
| `channels:history`, `groups:history` | Owned bound-thread replies in public/private channels |
| `reactions:write` | Receipt reaction on the exact triggering message |

Events are `app_mention`, `message.im`, `message.channels`, `message.groups`, and `agent_session_stopped`. Signed interactivity handles native OpenAPPA Allow/Deny. Phase 4 removes shortcuts, feedback, active-view context injection, unfurls and their `commands`/`links:read`/`links:write` scopes. Existing installations must import/reinstall; text in the manifest does not prove the token has the required permissions. A successful `reactions.add` is the receipt-scope acceptance check.

## One conversation and authorization

HMAC verifies the raw body/timestamp before normalization. The signed team/human identity resolves through DSQL, and every reply, link, review or Stop verifies thread ownership. Only mentions start channel conversations; plain channel messages must reply to an existing binding. DMs can start their own thread. Use `@bot link <application thread UUID>` to link an owned Web conversation explicitly. Sharing a URL does not invoke an unfurl workflow.

Processing status, exact-message receipts, chunk task cards, canonical final responses and Stop are enabled as one baseline. Source permalinks are included when `PUBLIC_APP_URL` is configured. Failed card startup falls back to a normal final reply. An OpenAPPA-required review shows exact tool/arguments and policy explanation with Allow/Deny; it records a ruling for native resolution, never a direct CMA approval. Clarification is shown as text and answered in authenticated Web. There are no capability flags.

```bash
export DSQL_ENDPOINT="$(terraform -chdir=infra/app output -raw dsql_endpoint)"
npm run db:map-slack -- --team-id T01234567 --user-id U01234567
```

Run with concrete AWS credentials and `AWS_REGION=us-west-2`. Optional `SLACK_TEAM_ID`/`SLACK_USER_ID` narrow development traffic but never authorize it; normally leave them unset.

## API-only sandbox testing

Configure local `SLACK_USER_TOKEN`, `SLACK_BOT_TOKEN`, `E2E_LIVE_SLACK_CHANNEL_ID`, Web credentials and AWS/DSQL access. User token scopes need `chat:write` and the channel's history scope; both users/bot must access the channel. `RUN_LIVE_E2E=1 npm run test:live -- all` validates account, configuration, scopes, identity ownership and channel access before genuine delivery. It does not deploy, provision, remap or log in. The user token stays local and is never synced to Secrets Manager or Lambda.

Slack may attach bot/app metadata to a user-token post. If so, configure the exact `SLACK_USER_BOT_ID` plus `SLACK_USER_APP_ID` pair and deploy it to ingress. Both must match; the human still needs its DSQL mapping. Leaving them unset disables this narrow exception.

All agent-run Slack testing uses CLI/API helpers. Never ask the user to open Slack, log in, send messages, click buttons or inspect the UI. Missing/revoked credentials, scopes or channel access are reported directly with secret-safe diagnostics. Signed fixtures do not prove native clicks, modals or client rendering. The current live policy smoke checks automatic policy; new HITL live acceptance requires its own explicitly authorized policy rollout. See [testing](testing.md).

Troubleshoot no ingress by checking Socket Mode and request/event configuration; ignored ingress by metadata/identity/ownership; missing output by scheduler/push/projection receipts and Slack API errors. Never log message bodies or credentials. Reference: [Slack request signing](https://api.slack.com/docs/verifying-requests-from-slack), [streaming API](https://docs.slack.dev/reference/methods/chat.startStream/).
