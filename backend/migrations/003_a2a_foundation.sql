CREATE TABLE IF NOT EXISTS agent_threads (
    thread_id UUID PRIMARY KEY,
    principal_id UUID NOT NULL REFERENCES principals(principal_id),
    agent_id TEXT NOT NULL,
    context_id TEXT,
    context_state TEXT NOT NULL DEFAULT 'uninitialized'
        CHECK (context_state IN ('uninitialized', 'initializing', 'ready')),
    context_init_claim_id UUID,
    context_init_lease_expires_at TIMESTAMPTZ,
    CHECK (
        (context_state = 'uninitialized' AND context_id IS NULL
            AND context_init_claim_id IS NULL AND context_init_lease_expires_at IS NULL)
        OR (context_state = 'initializing' AND context_id IS NULL
            AND context_init_claim_id IS NOT NULL AND context_init_lease_expires_at IS NOT NULL)
        OR (context_state = 'ready' AND context_id IS NOT NULL
            AND context_init_claim_id IS NULL AND context_init_lease_expires_at IS NULL)
    ),
    title TEXT,
    last_task_id TEXT,
    last_task_state TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_at TIMESTAMPTZ,
    UNIQUE (agent_id, context_id)
);

CREATE TABLE IF NOT EXISTS thread_surface_bindings (
    binding_id UUID PRIMARY KEY,
    thread_id UUID NOT NULL REFERENCES agent_threads(thread_id),
    surface TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    external_thread_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (surface, tenant_id, external_thread_id)
);

CREATE TABLE IF NOT EXISTS agent_tasks (
    task_binding_id UUID PRIMARY KEY,
    thread_id UUID NOT NULL REFERENCES agent_threads(thread_id),
    agent_id TEXT NOT NULL,
    client_message_id TEXT NOT NULL,
    task_id TEXT,
    submission_status TEXT NOT NULL
        CHECK (submission_status IN ('pending', 'bound', 'failed')),
    CHECK (
        (submission_status = 'pending' AND task_id IS NULL)
        OR (submission_status = 'bound' AND task_id IS NOT NULL)
        OR submission_status = 'failed'
    ),
    last_seen_task_state TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (thread_id, client_message_id),
    UNIQUE (agent_id, task_id)
);

CREATE TABLE IF NOT EXISTS cma_contexts (
    context_id TEXT PRIMARY KEY,
    creation_message_id TEXT NOT NULL UNIQUE,
    cma_session_id TEXT UNIQUE,
    lifecycle_state TEXT NOT NULL
        CHECK (lifecycle_state IN ('initializing', 'ready', 'failed')),
    CHECK (
        (lifecycle_state = 'initializing' AND cma_session_id IS NULL)
        OR (lifecycle_state = 'ready' AND cma_session_id IS NOT NULL)
        OR lifecycle_state = 'failed'
    ),
    next_sequence BIGINT NOT NULL DEFAULT 1 CHECK (next_sequence >= 1),
    active_task_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cma_tasks (
    task_id TEXT PRIMARY KEY,
    context_id TEXT NOT NULL REFERENCES cma_contexts(context_id),
    message_id TEXT NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence >= 1),
    a2a_state TEXT NOT NULL,
    internal_state TEXT NOT NULL,
    cma_input_event_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (context_id, message_id),
    UNIQUE (context_id, sequence)
);
