# A2A and AG-UI foundation

Phase 1 added protocol contracts and inert persistence. Phase 2 adds a local HTTP+JSON CMA controller, durable scheduler, provider event reconstruction, and controller-only AWS execution primitives. Existing web and Slack handlers continue to call CMA directly. No AG-UI bridge or public A2A ingress is deployed.

## Protocol roles

- **AG-UI:** human interface to application interaction.
- **A2A:** application surfaces and bridge to agent controllers.
- **MCP:** agent access to capabilities and tools.

The intended web path is `assistant-ui → AG-UI bridge → A2A → CMA controller → ManagedAgentClient → CMA`. Slack will call A2A in a later phase.

## Ownership and identity

The application owns principals, thread identity, agent selection, surface bindings, logical sends, routing, and lightweight projections. CMA owns transcripts and execution state; the controller owns task ordering and routing metadata. The application database never stores message bodies, tool outputs, or reasoning.

`thread_id` is the application conversation identity. A2A `contextId` is an opaque controller conversation identity and A2A `taskId` is an opaque unit of work. Future AG-UI `threadId` equals application `thread_id`; AG-UI `runId` equals A2A `taskId`. A thread acquires its context lazily, protected by a lease and claim token.

## Controller compatibility profile

`async-copilot-controller/v1` requires HTTP+JSON, task streaming/subscription, durable task and message history, CancelTask, push notifications, durable `messageId` deduplication, and conversational final answers as A2A Messages. Artifacts represent produced objects, files, or structured outputs.

Base A2A `message/send` waits for a terminal or input-required boundary unless the caller sets `returnImmediately=true`. A future application preference for nonblocking calls belongs in a negotiated application profile; the full profile is not advertised in Phase 2.

`human-input/v1`, concurrent tasks, queueing behind input required, and artifact production are separately declared behavioral capabilities. `human-input/v1` carries provider-neutral tool approval or clarification requests while the A2A task is `INPUT_REQUIRED`; generic A2A clients can still recognize the task state without rendering the extension.

Clients activate `human-input/v1` with the A2A extension header. Without activation, the status message is plain text and has no structured approval metadata. Continuations require both activation and the extension URI on the Message.

The CMA controller serializes turns FIFO, blocks ordinary new turns during `INPUT_REQUIRED`, and supports human input. Cancellation is task scoped. Phase 2 supports polling and streaming; the full profile and private push sink arrive in Phase 3. Slack and Anthropic webhook remain public ingress; controller services remain private.

## Migration boundary

The application-owned tables remain unused by current handlers. Controller-private tables now hold ordering, scheduler leases, CMA event boundaries, and HITL decisions. Undelivered user text is held temporarily in private encrypted S3 and deleted after CMA accepts it. The final cutover may reset the environment and remove the legacy schema.

The controller scheduler has a dedicated SQS queue and dead-letter queue. A daily sweep removes only unreferenced old S3 inputs and alarms on stale referenced inputs. Ambiguous CMA create, send, confirmation, and interrupt outcomes remain held for reconciliation and raise a CloudWatch alarm.
