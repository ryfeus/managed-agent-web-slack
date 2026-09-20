from __future__ import annotations

from pathlib import Path

from managed_agents_app.db import IngressClaim, ProjectionClaim, repositories
from managed_agents_app.operations import MIGRATIONS, REPOSITORY_ROOT


class FakeCursor:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.current = None
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.statements.append((query, params))
        if (
            query.lstrip().startswith(("SELECT", "UPDATE"))
            and "RETURNING" in query
            or query.lstrip().startswith("SELECT")
        ):
            self.current = next(self.rows)

    def fetchone(self):
        return self.current


class FakeConnection:
    def __init__(self, cursor):
        self.value = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self.value


def install(monkeypatch, rows):
    cursor = FakeCursor(rows)
    monkeypatch.setattr(repositories, "connect", lambda *_: FakeConnection(cursor))
    return cursor


def test_ingress_lease_acquire_complete_and_busy(monkeypatch, config) -> None:
    db = repositories.Database(config)
    install(monkeypatch, [{"status": "failed", "lease_expires_at": None}, {"ingress_id": "i1"}])
    assert db.claim_ingress("slack", "Ev1") == IngressClaim.ACQUIRED

    install(monkeypatch, [{"status": "sent", "lease_expires_at": None}])
    assert db.claim_ingress("slack", "Ev1") == IngressClaim.COMPLETED

    install(monkeypatch, [{"status": "sending", "lease_expires_at": "future"}, None])
    assert db.claim_ingress("slack", "Ev1") == IngressClaim.BUSY


def test_stream_lease_recovery_and_completion(monkeypatch, config) -> None:
    db = repositories.Database(config)
    install(monkeypatch, [{"status": "pending"}, {"binding_id": "b1"}])
    assert db.claim_stream("b1", "Ev1", "sesn_1") == ProjectionClaim.ACQUIRED

    install(monkeypatch, [{"status": "completed"}])
    assert db.claim_stream("b1", "Ev1", "sesn_1") == ProjectionClaim.COMPLETED

    install(monkeypatch, [{"request_event_id": "Ev1", "slack_message_ts": "10.2"}])
    assert db.find_recoverable_stream("b1", "sesn_1") == ("Ev1", "10.2")

    cursor = install(monkeypatch, [None])
    assert db.find_recoverable_stream("b1", "sesn_1") is None
    recovery_query = cursor.statements[0][0]
    assert "status = 'fallback'" in recovery_query
    assert "lease_expires_at <= CURRENT_TIMESTAMP" in recovery_query

    cursor = install(monkeypatch, [{"?column?": 1}])
    assert db.has_active_stream("b1", "sesn_1") is True
    active_query = cursor.statements[0][0]
    assert "status = 'streaming'" in active_query
    assert "lease_expires_at > CURRENT_TIMESTAMP" in active_query

    cursor = install(monkeypatch, [])
    db.complete_recovered_stream("b1", "Ev1", "evt_final", "10.2")
    assert len(cursor.statements) == 2
    assert "slack_response_streams" in cursor.statements[0][0]
    assert "projection_events" in cursor.statements[1][0]


def test_slack_feature_schema_keeps_metadata_not_transcripts() -> None:
    migration = (Path(__file__).parents[1] / "migrations" / "002_slack_agent_features.sql").read_text()
    assert "interaction_id TEXT NOT NULL UNIQUE" in migration
    assert "slack_response_streams" in migration
    assert "response_text" not in migration
    assert "message_body" not in migration


def test_operation_paths_resolve_repository_files() -> None:
    assert (REPOSITORY_ROOT / ".env.example").is_file()
    assert (MIGRATIONS / "001_initial.sql").is_file()
    assert (MIGRATIONS / "002_slack_agent_features.sql").is_file()
