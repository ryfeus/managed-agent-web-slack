-- Preserve committed policy decisions, resolved rows and every attempted action.
-- Without verified legacy publication, only an intentional cancellation-deny may
-- remain resolving and unattempted. Never reopen an uncertain provider action.
UPDATE cma_input_requests SET status = 'pending', decision = NULL, updated_at = CURRENT_TIMESTAMP
WHERE kind = 'tool_approval' AND status = 'resolving' AND confirmation_attempted_at IS NULL
AND NOT EXISTS (
    SELECT 1 FROM cma_tasks t JOIN cma_contexts c ON c.context_id = t.context_id
    WHERE t.task_id = cma_input_requests.task_id
    AND (
        (cma_input_requests.decision IN ('allow', 'deny')
         AND cma_input_requests.policy_snapshot_revision > 0
         AND c.policy_snapshot_revision >= cma_input_requests.policy_snapshot_revision
         AND c.policy_snapshot_key IS NOT NULL AND c.policy_snapshot_key <> '')
        OR (cma_input_requests.decision = 'deny' AND t.cancel_requested_at IS NOT NULL)
    )
);

-- Drop this first: its absence tells the migration runner that normalization has
-- already committed if execution is interrupted between DSQL DDL statements.
ALTER TABLE cma_input_requests DROP COLUMN IF EXISTS policy_snapshot_revision;
ALTER TABLE cma_contexts DROP COLUMN IF EXISTS policy_snapshot_key;
ALTER TABLE cma_contexts DROP COLUMN IF EXISTS policy_snapshot_revision;
