DROP TABLE IF EXISTS a2a_task_event_receipts;
DROP TABLE IF EXISTS a2a_task_observation_claims;
ALTER TABLE agent_tasks DROP COLUMN IF EXISTS last_seen_task_state;
