-- Only control metadata and opaque temporary-object references, never tool payloads.
CREATE TABLE IF NOT EXISTS cma_tool_executions (
    invocation_id UUID PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES cma_tasks(task_id),
    cma_event_id TEXT NOT NULL,
    call_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'result_ready', 'admitted', 'withheld', 'delivering', 'resolved')),
    authorization_object_key TEXT,
    result_object_key TEXT NOT NULL,
    result_digest TEXT,
    control_code TEXT,
    execution_attempted_at TIMESTAMPTZ,
    delivery_attempted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (task_id, cma_event_id)
);
