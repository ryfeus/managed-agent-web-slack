CREATE TABLE IF NOT EXISTS agent_feedback (
    feedback_id UUID PRIMARY KEY,
    interaction_id TEXT NOT NULL UNIQUE,
    principal_id UUID NOT NULL REFERENCES principals(principal_id),
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    managed_event_id TEXT,
    surface TEXT NOT NULL,
    external_message_id TEXT,
    rating TEXT NOT NULL CHECK (rating IN ('positive', 'negative')),
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS slack_response_streams (
    binding_id UUID NOT NULL REFERENCES surface_bindings(binding_id),
    request_event_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'streaming', 'completed', 'fallback')),
    slack_message_ts TEXT,
    last_managed_event_id TEXT,
    final_managed_event_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (binding_id, request_event_id)
);
