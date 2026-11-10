-- Immutable private SQLite objects; no policy payloads enter DSQL.
ALTER TABLE cma_contexts ADD COLUMN IF NOT EXISTS policy_checkpoint_key TEXT;
ALTER TABLE cma_contexts ADD COLUMN IF NOT EXISTS policy_checkpoint_revision BIGINT;
ALTER TABLE cma_contexts ALTER COLUMN policy_checkpoint_revision SET DEFAULT 0;
UPDATE cma_contexts SET policy_checkpoint_revision = 0 WHERE policy_checkpoint_revision IS NULL;
ALTER TABLE cma_contexts ADD COLUMN IF NOT EXISTS policy_digest TEXT;
