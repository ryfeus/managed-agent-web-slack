ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS creation_request_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS agent_threads_creation_request_unique
    ON agent_threads (principal_id, creation_request_id);

CREATE TABLE IF NOT EXISTS cma_clarification_requests (
    request_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES cma_tasks(task_id),
    cma_event_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'clarification' CHECK (kind = 'clarification'),
    status TEXT NOT NULL CHECK (status IN ('pending', 'resolving', 'resolved')),
    answer_object_key TEXT,
    response_attempted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMPTZ,
    UNIQUE (task_id, cma_event_id)
);
