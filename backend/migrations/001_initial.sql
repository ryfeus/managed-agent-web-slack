CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS principals (
    principal_id UUID PRIMARY KEY,
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS external_identities (
    identity_id UUID PRIMARY KEY,
    principal_id UUID NOT NULL REFERENCES principals(principal_id),
    provider TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (provider, tenant_id, external_user_id)
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    session_id TEXT PRIMARY KEY,
    principal_id UUID NOT NULL REFERENCES principals(principal_id),
    agent_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    created_by_surface TEXT NOT NULL CHECK (created_by_surface IN ('web', 'slack')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS surface_bindings (
    binding_id UUID PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    surface TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    external_thread_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (surface, tenant_id, external_thread_id)
);

CREATE TABLE IF NOT EXISTS ingress_events (
    ingress_id UUID PRIMARY KEY,
    surface TEXT NOT NULL,
    external_event_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('received', 'sending', 'sent', 'failed')),
    session_id TEXT REFERENCES agent_sessions(session_id),
    managed_event_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (surface, external_event_id)
);

CREATE TABLE IF NOT EXISTS projection_events (
    binding_id UUID NOT NULL REFERENCES surface_bindings(binding_id),
    managed_event_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('posting', 'posted', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    slack_message_ts TEXT,
    projected_at TIMESTAMPTZ,
    PRIMARY KEY (binding_id, managed_event_id)
);
