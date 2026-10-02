ALTER TABLE cma_contexts ADD COLUMN IF NOT EXISTS scheduler_claim_id UUID;
ALTER TABLE cma_contexts ADD COLUMN IF NOT EXISTS scheduler_lease_expires_at TIMESTAMPTZ;
ALTER TABLE cma_tasks ADD COLUMN IF NOT EXISTS input_object_key TEXT;
ALTER TABLE cma_tasks ADD COLUMN IF NOT EXISTS cma_predecessor_event_id TEXT;
ALTER TABLE cma_tasks ADD COLUMN IF NOT EXISTS cma_terminal_event_id TEXT;
ALTER TABLE cma_tasks ADD COLUMN IF NOT EXISTS dispatch_attempts INTEGER;
ALTER TABLE cma_tasks ADD COLUMN IF NOT EXISTS dispatch_attempted_at TIMESTAMPTZ;
ALTER TABLE cma_tasks ADD COLUMN IF NOT EXISTS next_dispatch_at TIMESTAMPTZ;
ALTER TABLE cma_tasks ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;
ALTER TABLE cma_tasks ADD COLUMN IF NOT EXISTS cancel_attempted_at TIMESTAMPTZ;
ALTER TABLE cma_tasks ADD COLUMN IF NOT EXISTS held_reason TEXT;
CREATE TABLE IF NOT EXISTS cma_input_requests (
    request_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES cma_tasks(task_id),
    cma_event_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('tool_approval')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'resolving', 'resolved')),
    decision TEXT CHECK (decision IN ('allow', 'deny')),
    confirmation_attempted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMPTZ,
    UNIQUE (task_id, cma_event_id)
);
