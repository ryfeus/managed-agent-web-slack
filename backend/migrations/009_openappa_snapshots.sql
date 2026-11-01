ALTER TABLE cma_contexts ADD COLUMN IF NOT EXISTS policy_snapshot_key TEXT;
-- DSQL requires default installation separately from ADD COLUMN. During a
-- rolling migration, an initial NULL revision is equivalent to zero.
ALTER TABLE cma_contexts ADD COLUMN IF NOT EXISTS policy_snapshot_revision BIGINT;
ALTER TABLE cma_contexts ALTER COLUMN policy_snapshot_revision SET DEFAULT 0;
UPDATE cma_contexts SET policy_snapshot_revision = 0 WHERE policy_snapshot_revision IS NULL;
ALTER TABLE cma_input_requests ADD COLUMN IF NOT EXISTS policy_snapshot_revision BIGINT;

-- Unattempted legacy human decisions must be reevaluated by the canonical policy.
-- Attempted confirmations retain their existing provider-history/ambiguity fencing.
UPDATE cma_input_requests SET status = 'pending', decision = NULL
WHERE status = 'resolving' AND confirmation_attempted_at IS NULL AND policy_snapshot_revision IS NULL;
