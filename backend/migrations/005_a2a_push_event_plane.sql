CREATE TABLE IF NOT EXISTS cma_push_configs (
    task_id TEXT NOT NULL REFERENCES cma_tasks(task_id),
    config_id TEXT NOT NULL,
    url TEXT NOT NULL,
    last_delivered_fingerprint TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    last_delivered_at TIMESTAMPTZ,
    delivery_claim_id UUID,
    delivery_lease_expires_at TIMESTAMPTZ,
    permanent_failure_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (task_id, config_id)
);

CREATE TABLE IF NOT EXISTS a2a_task_event_receipts (
    receipt_id UUID PRIMARY KEY,
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    thread_id UUID NOT NULL REFERENCES agent_threads(thread_id),
    context_id TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    observed_task_state TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMPTZ,
    UNIQUE (agent_id, delivery_id)
);

CREATE TABLE IF NOT EXISTS a2a_task_observation_claims (
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    claim_id UUID,
    lease_expires_at TIMESTAMPTZ,
    PRIMARY KEY (agent_id, task_id)
);
