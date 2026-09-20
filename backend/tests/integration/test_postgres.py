import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from managed_agents_app.config import load_config
from managed_agents_app.db import Database, IngressClaim, ProjectionClaim
from managed_agents_app.db.connection import connect
from managed_agents_app.testing.factory import validate_local_database

pytestmark = pytest.mark.skipif(os.getenv("RUN_POSTGRES_TESTS") != "1", reason="Run through npm run e2e")


@pytest.fixture
def db():
    config = load_config()
    validate_local_database(config)
    return Database(config)


def test_migrations_are_repeatable_and_transactions_rollback(db):
    from managed_agents_app.operations import migrate

    migrate()
    with connect(db.config) as conn:
        assert len(conn.execute("SELECT version FROM schema_migrations").fetchall()) == 2
    with pytest.raises(RuntimeError), connect(db.config) as conn:
        conn.execute("INSERT INTO principals(principal_id) VALUES ('00000000-0000-4000-8000-000000000099')")
        raise RuntimeError("rollback")
    with connect(db.config) as conn:
        assert not conn.execute("SELECT * FROM principals").fetchall()


def test_real_transaction_claim_and_binding_races(db):
    principal = db.config.dev_principal_id
    db.ensure_principal(principal)
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=3)
        return db.claim_ingress("slack", "concurrent")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        assert sorted(f.result().value for f in futures) == sorted(
            [IngressClaim.ACQUIRED.value, IngressClaim.BUSY.value]
        )
    db.complete_ingress("slack", "concurrent", None)
    assert db.claim_ingress("slack", "concurrent") == IngressClaim.COMPLETED

    def bind(session):
        barrier.wait(timeout=3)
        return db.register_and_bind(session, principal, "agent_e2e", "env_e2e", "T001", "C001", "1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(bind, f"sesn_{i}") for i in range(2)]
        bindings = [f.result() for f in futures]
    assert bindings[0]["session_id"] == bindings[1]["session_id"]
    binding = bindings[0]
    assert (
        db.claim_stream(str(binding["binding_id"]), "req", str(binding["session_id"]))
        == ProjectionClaim.ACQUIRED
    )
    assert db.has_active_stream(str(binding["binding_id"]), str(binding["session_id"]))
    assert db.find_recoverable_stream(str(binding["binding_id"]), str(binding["session_id"])) is None
    with connect(db.config) as conn:
        conn.execute(
            "UPDATE slack_response_streams SET slack_message_ts='2.0', "
            "lease_expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second'"
        )
    assert db.find_recoverable_stream(str(binding["binding_id"]), str(binding["session_id"])) == (
        "req",
        "2.0",
    )
    db.complete_recovered_stream(str(binding["binding_id"]), "req", "final", "2.0")
    assert db.claim_projection(str(binding["binding_id"]), "final") == ProjectionClaim.COMPLETED


def test_reset_cancels_subscriptions_and_webhook_respects_real_lease(db):
    from fastapi.testclient import TestClient

    from managed_agents_app.handlers.slack_projector import handle_domain_event
    from managed_agents_app.local_api import create_app
    from managed_agents_app.testing.factory import create_runtime

    runtime = create_runtime(db.config)
    client = TestClient(create_app(runtime))
    assert client.post("/_test/reset").status_code == 200
    session = runtime.agent.create_session(
        principal_id=db.config.dev_principal_id, surface="web", creation_request_id="lease-test"
    )
    subscription = runtime.agent.stream_events(session.id)
    binding = db.register_and_bind(
        session.id, db.config.dev_principal_id, "agent_e2e", "env_e2e", "T001", "C001", "1"
    )
    binding_id = str(binding["binding_id"])
    assert db.claim_stream(binding_id, "request", session.id) == ProjectionClaim.ACQUIRED
    client.post(
        "/_test/agent/emit",
        json={
            "session_id": session.id,
            "event": {"type": "agent.message", "content": [{"type": "text", "text": "Final"}]},
        },
    )
    with pytest.raises(RuntimeError, match="still active"):
        handle_domain_event(
            runtime,
            {
                "detail-type": "ManagedAgentSessionChanged",
                "detail": {
                    "sessionId": session.id,
                    "webhookEventId": "webhook",
                    "webhookType": "session.status_idled",
                },
            },
        )
    assert client.get("/_test/state").json()["slack"]["messages"] == []
    assert client.post("/_test/reset").status_code == 200
    assert list(subscription) == []
    state = client.get("/_test/state").json()
    assert state["database"]["agent_sessions"] == []
    assert state["agent"]["sessions"] == []
    assert state["events"] == {"pending": [], "history": []}
    recreated = runtime.agent.create_session(
        principal_id=db.config.dev_principal_id, surface="web", creation_request_id="fresh"
    )
    assert recreated.id == session.id
