# Slack setup

Use a Slack Developer Program sandbox for acceptance testing. It supports the Slack agent capabilities used here without requiring a paid production workspace.

## Information required from the Slack sandbox

- Rotated bot token (`xoxb-…`) and signing secret, stored only in `.env`/Secrets Manager.
- Workspace/team ID for the sandbox.
- Human test-member ID.
- One public and one private test-channel ID.

Team, member, and channel IDs are routing/acceptance configuration, not authentication credentials. The app-level token is not used because Socket Mode remains disabled. Rotate any Slack bot token, app token, signing secret, or client secret previously exposed during inspection.

## Install the manifest

1. Run `npm run slack:manifest` after deployment, or pass
   `--public-app-url https://example.cloudfront.net` to the renderer.
2. Import the generated `.generated/slack-manifest.yaml` in the sandbox app.
3. Keep Socket Mode disabled.
4. Confirm the Events API URL is the Terraform `slack_events_url` output.
5. Confirm the interactivity URL is `slack_interactions_url`.
6. Confirm the rendered deployment host is an unfurl domain.
7. Install or reinstall the app after every scope/event/feature change.
8. Invite the app to both acceptance channels.

The manifest requests:

| Scope | Why it is required |
| --- | --- |
| `assistant:write` | Agent View session status, lifecycle, and native agent UI |
| `chat:write` | Replies and streamed responses |
| `app_mentions:read` | Top-level channel prompts |
| `im:history` | Agent View/DM continuity and shortcut context |
| `channels:history` | Public-channel bound replies and shortcut context |
| `groups:history` | Private-channel bound replies and shortcut context |
| `commands` | Message shortcuts and interactive controls |
| `links:read` | Receive authorized session-link shares |
| `links:write` | Render session unfurls |
| `reactions:write` | Add the optional idempotent receipt reaction to the triggering message |

Subscribed events are `app_mention`, `message.im`, `message.channels`, `message.groups`, `app_home_opened`, `app_context_changed`, `agent_session_stopped`, and `link_shared`. App-home/context events that need no durable action are acknowledged and ignored; no channel mirror is built.

## Identity and authorization

1. The signing secret authenticates each unmodified HTTP request body and enforces Slack's replay window.
2. The verified payload supplies `team_id` and the acting human `user_id`.
3. Agent Input resolves `(provider='slack', team_id, user_id)` through DSQL.
4. Every protected stop, approval, feedback, shortcut, link, and unfurl rechecks identity and session ownership.

Map the test human after DSQL migration:

```bash
export DSQL_ENDPOINT="$(terraform -chdir=infra/app output -raw dsql_endpoint)"
npm run db:map-slack -- --team-id T01234567 --user-id U01234567
```

`SLACK_TEAM_ID` and `SLACK_USER_ID` can temporarily narrow development traffic and let `db:seed` create one mapping. They are optional, do not prove identity, and should normally be unset after sandbox setup.

## Feature gates

All new capabilities default off so deployment can establish Python parity first:

```text
SLACK_BOUND_THREAD_REPLIES
SLACK_AGENT_VIEW_ENABLED
SLACK_STREAMING_ENABLED
SLACK_TOOL_APPROVALS_ENABLED
SLACK_FEEDBACK_ENABLED
SLACK_SHORTCUTS_ENABLED
SLACK_ACTIVE_CONTEXT_ENABLED
SLACK_UNFURLS_ENABLED
SLACK_RECEIPT_REACTION_ENABLED
SLACK_RECEIPT_REACTION
SLACK_SOURCE_LINKS_ENABLED
SLACK_TASK_CARDS_ENABLED
```

Set boolean gates to `true` individually in `.env` and redeploy. `SLACK_RECEIPT_REACTION` is a Slack reaction name without colons and defaults to `eyes`. Task cards require streaming; when task cards are enabled without streaming, they are disabled at runtime with a warning. `PUBLIC_APP_URL` should be the CloudFront origin without a trailing slash; the deployment wrapper can recover the existing Terraform output when it is omitted.

After adding `reactions:write` to the manifest, reinstall the app before enabling receipt reactions. Otherwise Slack returns `missing_scope` and EventBridge retries the isolated working-state projection.

## Expected behavior

- A top-level channel prompt must mention the app.
- A plain reply is accepted only in an already-bound thread.
- A top-level DM creates a session; later DM thread messages reuse it.
- Accepted requests immediately enter `processing` and can receive `:eyes:` on the exact message.
- Responses can appear as timeline task cards linked to the original Slack message.
- A task completes only after a final response with no pending approval; approvals leave it in progress and set the session to `suspended`.
- Slack stop sends an authorized Managed Agent `user.interrupt`.
- Pending tools render Allow/Deny actions; denial with a reason opens a modal.
- Feedback stores identifiers/rating only, never response text.
- Shortcuts fetch current authorized Slack context at execution time and do not persist it.
- Sharing `https://<cloudfront>/?session=sesn_…` unfurls only when the sharing identity owns the session.
- `link sesn_…` remains only as a compatibility escape hatch.
