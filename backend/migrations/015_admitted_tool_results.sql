-- Private immutable admitted payloads; raw outcomes remain separate and unchanged.
ALTER TABLE cma_tool_executions ADD COLUMN IF NOT EXISTS admitted_result_object_key TEXT;
ALTER TABLE cma_tool_executions ADD COLUMN IF NOT EXISTS admitted_result_digest TEXT;
