-- Review lifecycle uses the existing status domain: pending without a digest needs
-- evaluation; pending + digest awaits a human; pending + ruling needs native
-- resolution. Only the scheduler may move it to resolving/resolved.
ALTER TABLE cma_input_requests ADD COLUMN IF NOT EXISTS review_digest TEXT;
ALTER TABLE cma_input_requests ADD COLUMN IF NOT EXISTS review_authority TEXT;
ALTER TABLE cma_input_requests ADD COLUMN IF NOT EXISTS review_object_key TEXT;
-- DSQL does not support constraints on ALTER TABLE ADD COLUMN. The fenced
-- repository transition validates the allow/deny domain before writing.
ALTER TABLE cma_input_requests ADD COLUMN IF NOT EXISTS human_ruling TEXT;
