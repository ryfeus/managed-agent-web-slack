# Personal MCP access

CloudFront routes `/mcp` to a separate Node 24 Web Function. Cognito authenticates the human; DSQL maps the exact issuer/subject to an existing principal. The gateway verifies thread ownership and Task binding before calling private A2A. Tokens are never forwarded to A2A. The gateway has no Anthropic credential, policy storage access or confirmation port.

The official TypeScript SDK 2.4 serves 2026-07-28 and stateless legacy MCP requests. Each call authenticates independently; no sticky sessions or MCP session database are required.

## Tools and local tests

Tools are `list_threads`, `create_thread`, `send_message`, `list_tasks`, `get_task`, `wait_task` and `stop_task`. Create/send require a UUID `clientRequestId`: retain it across uncertain retries. A failed response or push-registration failure can follow accepted execution. Never generate a replacement ID for the same input. MCP message IDs use a separate `mcp:` namespace.

Send returns a Task ID promptly. Wait observes canonical status for at most 20 seconds. Read returns public text with message pagination and `textOffset` for long messages. Lists reflect current state rather than immutable snapshots. Disconnect never cancels; explicit Stop targets a bound Task. Human review and clarification return `needsHumanInput` and the Web `threadUrl`. There is no MCP approval tool, preview or reasoning projection.

Stop is asynchronous for running Tasks: its response can still show the previous state while the scheduler handles the cancellation request. Poll canonical status until `TASK_STATE_CANCELED`; an interim human-input state does not require submitting a ruling or answer.

`npm run dev` prints the local MCP URL and runs isolated PostgreSQL/fake CMA with an ephemeral JWT key. The loopback MCP endpoint `GET /_test/token` issues a five-minute test token; this route does not exist in production. Local signed JWT fixtures do not establish Cognito or native client OAuth compatibility.

Use `npm run e2e -- --grep MCP` for PostgreSQL and SDK-to-gateway acceptance, including retries, disconnect and canonical Web history. `npm run e2e:production -- --grep MCP` exercises the packaged gateway and static Web build. Run `npm run verify:full` and `npm run secrets:check` before completion.

## AWS rollout

Infrastructure defaults to `mcp_enabled=false`. Use the existing HTTPS application origin and `us-west-2`. The gateway reuses the Web Function's VPC egress connector. CloudFront preserves Authorization through `always-amz-auth`; responses are `no-store`. OAuth metadata is at `/.well-known/oauth-protected-resource/mcp`.

Prepare `MCP_ENABLED=true npm run deploy:plan -- --application-only`, review the plan, then explicitly deploy with `MCP_ENABLED=true npm run deploy -- --application-only`. CMA definitions are unchanged. This mode does not grant database roles or link users.

Terraform supplies `mcp_url`, `mcp_user_pool_id`, `mcp_oauth_issuer`, `mcp_client_ids`, `mcp_runtime_role_arn` and `dsql_endpoint`. Cognito uses public clients, authorization code with PKCE, 15-minute access tokens, 30-day refresh tokens and disabled self-registration. Provision your personal user through Cognito administration; initial sign-in/password setup is user-operated. Passwords and tokens never enter Terraform configuration or state.

After SSO login and STS verification, export concrete credentials only in the administration shell. Replace uppercase placeholders below with outputs and your existing principal:

```bash
export AWS_PROFILE=default AWS_SDK_LOAD_CONFIG=1
aws sts get-caller-identity --profile "$AWS_PROFILE"
eval "$(aws configure export-credentials --profile "$AWS_PROFILE" --format env)"
npm run db:mcp-admin -- --endpoint DSQL_ENDPOINT --expected-account-id ACCOUNT_ID grant-role --role-arn MCP_RUNTIME_ROLE_ARN
npm run db:mcp-admin -- --endpoint DSQL_ENDPOINT --expected-account-id ACCOUNT_ID link-user --user-pool-id USER_POOL_ID --username YOUR_COGNITO_USERNAME --principal-id EXISTING_PRINCIPAL_UUID
```

The admin command never reads `.env`. `mcp_runtime` receives only identity lookup and thread/Task-binding grants. Never include its IAM role in `DSQL_RUNTIME_ROLE_ARNS`: that list grants broad `app_runtime` access. Linking reads the user's subject from Cognito, requires an existing principal and refuses reassignment. Delete the exact identity mapping to revoke application access immediately; Cognito sign-out alone can leave issued JWTs valid until expiry.

## Claude Code and Codex

Use `mcp_url` and the matching public client ID. No client secret is needed. Clients must send the MCP resource indicator; the gateway verifies that Cognito's access-token audience matches the MCP URL and that the `invoke` scope is present. It rejects ID tokens. Use a Claude Code release whose `claude mcp add --help` lists `--client-id` and `--callback-port`.

```bash
claude mcp add --transport http --client-id CLAUDE_CLIENT_ID --callback-port 8766 managed-agent MCP_URL
claude mcp login managed-agent
codex mcp add managed-agent --url MCP_URL --oauth-client-id CODEX_CLIENT_ID --oauth-resource MCP_URL
```

Set `callback_port = 8767` under `[mcp_servers.managed-agent.oauth]` in the local Codex configuration, then run `codex mcp login managed-agent`. Register the **exact callback reported by the installed client** before finishing login. Defaults are `http://localhost:8766/callback` for Claude and `http://127.0.0.1:8767/callback` for Codex. Codex versions using a server-specific callback path need that path added through `mcp_codex_callback_urls`; Claude has `mcp_claude_callback_urls`. Cognito requires exact matches. Local OAuth configuration/tokens stay outside this repository.

## Acceptance and rollback

After granting the runtime role, run `AWS_PROFILE=default EXPECTED_AWS_ACCOUNT_ID=ACCOUNT_ID RUN_LIVE_MCP=1 npm run test:mcp-live` for the standalone SDK harness. It verifies STS and Terraform outputs, provisions a disposable Cognito user without sending email, and tests real authorization-code/PKCE login and refresh. It checks unlinked/foreign access rejection, all seven tools, retry uniqueness, disconnect/reconnect, canonical Web history and explicit Stop after clarification. It then revokes tokens, removes the test identity and archives its threads while retaining canonical Task evidence. Passwords and tokens remain in private process pipes; `.generated/mcp-live-report.json` contains only metadata. This command is never included in local verification.

After deployment, test both clients' real login, refresh, discovery and tool calls. Verify direct-origin rejection, CloudFront Authorization forwarding on GET/POST, unlinked/cross-thread rejection and shared Web history. Inspect metadata-only CloudWatch events and existing Task metadata; never log payloads or credentials.

Revoke access by unlinking the identity first, then use a reviewed deployment to remove routing or restore the previous artifact. Turning `mcp_enabled` off plans removals and is not an emergency authentication switch. Preserve Cognito deletion protection and retained Web Function revisions.

References: [MCP authorization](https://modelcontextprotocol.io/specification/latest/basic/authorization), [Cognito resource binding](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-define-resource-servers.html#resource-binding), [CloudFront authorization](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-lambda-web.html), [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), [Claude Code MCP](https://code.claude.com/docs/en/mcp).
