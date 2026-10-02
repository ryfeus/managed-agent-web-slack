CREATE TABLE IF NOT EXISTS surface_ingress_events (
    ingress_id UUID PRIMARY KEY,
    surface TEXT NOT NULL,
    external_event_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('received', 'sending', 'sent', 'failed')),
    thread_id UUID REFERENCES agent_threads(thread_id),
    task_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    claim_id UUID,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (surface, external_event_id)
);
CREATE TABLE IF NOT EXISTS slack_task_projections (
    binding_id UUID NOT NULL REFERENCES thread_surface_bindings(binding_id),
    task_id TEXT NOT NULL,
    request_event_id TEXT,
    source_message_ts TEXT,
    initiator_user_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'streaming', 'completed', 'fallback')),
    slack_message_ts TEXT,
    last_task_state TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    claim_id UUID,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (binding_id, task_id)
);
CREATE TABLE IF NOT EXISTS slack_projection_items (
    binding_id UUID NOT NULL,
    task_id TEXT NOT NULL,
    item_kind TEXT NOT NULL CHECK (item_kind IN ('agent_message', 'human_input')),
    item_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('posting', 'posted', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    claim_id UUID,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    slack_message_ts TEXT,
    projected_at TIMESTAMPTZ,
    PRIMARY KEY (binding_id, task_id, item_kind, item_id)
);
CREATE TABLE IF NOT EXISTS thread_feedback (
    feedback_id UUID PRIMARY KEY,
    interaction_id TEXT NOT NULL UNIQUE,
    principal_id UUID NOT NULL REFERENCES principals(principal_id),
    thread_id UUID NOT NULL REFERENCES agent_threads(thread_id),
    task_id TEXT NOT NULL,
    message_id TEXT,
    surface TEXT NOT NULL,
    external_message_id TEXT,
    rating TEXT NOT NULL CHECK (rating IN ('positive', 'negative')),
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE cma_input_requests ADD COLUMN IF NOT EXISTS decision_reason_object_key TEXT;
