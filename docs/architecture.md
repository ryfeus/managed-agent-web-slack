# Architecture

## Ownership

- Claude Managed Agents: canonical conversation event log, execution history, session/thread status, and sandbox state.
- Aurora DSQL: principals, external identities, session authorization, surface bindings, ingress idempotency, feedback metadata, stream leases, and Slack projection receipts.
- EventBridge: asynchronous Slack messages/actions, projection requests, Managed Agent change notifications, and control replies.
- Python 3.14 Lambdas: stateless protocol and authorization adapters. Browser SSE uses FastAPI through Lambda Web Adapter response-stream mode.
- Slack and assistant-ui: input/projection surfaces over the same Claude session ID.

DSQL deliberately has no message or transcript table.

## Browser turn

```mermaid
sequenceDiagram
  participant UI as assistant-ui
  participant API as API Gateway/Lambda
  participant DB as Aurora DSQL
  participant Claude as Managed Agents
  participant Projector as Slack Projector
  UI->>API: Open SSE stream (signed cookie)
  API->>DB: Verify session ownership
  API->>Claude: Tail session events
  UI->>API: POST user message + request ID
  API->>DB: Claim ingress ID
  API->>Claude: Send user.message
  API->>DB: Mark ingress sent
  Claude-->>API: Persisted events + previews
  API-->>UI: SSE
```

## Slack inbound

```mermaid
sequenceDiagram
  participant Slack
  participant Ingress as Slack Ingress Lambda
  participant Bus as EventBridge
  participant Queue as SQS agent-input queue
  participant Worker as Agent Input Lambda
  participant DB as Aurora DSQL
  participant Claude as Managed Agents
  Slack->>Ingress: Signed event or interaction
  Ingress->>Ingress: Verify raw-body signature
  Ingress->>Bus: Typed Slack v1 event
  Ingress-->>Slack: Immediate 200
  Bus->>Queue: Normalized event
  Queue->>Worker: Durable delivery
  Worker->>DB: Resolve team + user identity
  Worker->>DB: Claim ingress / find binding
  Worker->>DB: Verify existing binding ownership
  Worker->>Bus: SlackWorkStarted v1
  Bus->>Projector: Working-state event
  Projector->>Slack: processing + optional exact-message reaction
  Worker->>Claude: Create/send/interrupt/confirm tool
  Worker->>DB: Persist session, binding, and receipt
```

## Slack projection

```mermaid
sequenceDiagram
  participant Claude as Managed Agents
  participant Hook as Webhook Lambda
  participant Bus as EventBridge
  participant Projector as Slack Projector
  participant DB as Aurora DSQL
  participant Slack
  Claude->>Hook: Signed session.status_idled
  Hook->>Bus: ManagedAgentSessionChanged
  Bus->>Projector: Session ID
  Projector->>DB: Find Slack bindings
  Projector->>Claude: Fetch canonical events
  Projector->>DB: Check projection receipt
  Projector->>Slack: Resolve source-message permalink
  Projector->>Slack: Start task card or text stream
  Projector->>Slack: Append response text (never reasoning)
  Projector->>Slack: Complete task or suspend for approval
  Projector->>DB: Record managed event + Slack timestamp
```

## Shared session

```mermaid
flowchart LR
  Browser["Browser thread"] --> Session["Claude session sesn_…"]
  Slack["Slack workspace/channel/thread"] --> Binding["DSQL surface binding"]
  Binding --> Session
  Session --> Log["Canonical Managed Agent event log"]
  Log --> Browser
  Log --> Projector["Slack projector"]
  Projector --> Slack
```

## Trust boundaries

- Slack signature verification authenticates Slack as the sender. The signed `team_id` and `user_id` are identity claims, not credentials.
- DSQL `external_identities(provider='slack', tenant_id, external_user_id)` authorizes the Slack identity as an application principal.
- Optional environment allowlists can reject development traffic before lookup but can never authorize an unmapped identity.
- Browser session cookies are signed and every session ID is checked against `agent_sessions` before an Anthropic API request.
- Anthropic and Slack service credentials remain server-side.

## Slack-native lifecycle

```mermaid
stateDiagram-v2
  [*] --> processing: authorized prompt
  processing --> suspended: tool confirmation required
  suspended --> processing: allow or deny
  processing --> active: response complete or stopped
  active --> processing: next bound-thread reply
  processing --> closed: Managed Agent terminated
  suspended --> closed: Managed Agent terminated
```

Live output uses `chat.startStream`, coalesced `chat.appendStream`, and `chat.stopStream`. Only text deltas are projected. Text streams use Slack's `markdown_text` request field; task-card streams use chunks for their full lifetime, including `markdown_text` response chunks and recovery finalization. The task source is attached when the stream starts, so recovery does not need to persist message content or a permalink. If streaming fails after Slack creates a message, `slack_response_streams` retains the timestamp and request ID so the webhook projector finalizes that message instead of posting a duplicate.

`SlackWorkStarted` is emitted only after ingress claim, identity authorization, input validation, and existing-binding ownership checks, but before Claude delivery. It contains routing identifiers and the exact triggering message timestamp—never message text. Projector failures retry independently and cannot cause a second Claude input.

Shortcuts fetch the authorized Slack thread at execution time and pass it to Anthropic as explicitly untrusted reference context. Message bodies are never persisted in DSQL. Active context is similarly ephemeral and feature-gated.
