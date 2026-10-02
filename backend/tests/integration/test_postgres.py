import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from psycopg import sql
from psycopg.errors import CheckViolation

from managed_agents_app.cma_controller.repository import CmaControllerRepository, ControllerIdentityConflict
from managed_agents_app.config import load_config
from managed_agents_app.db import Database
from managed_agents_app.db.connection import connect
from managed_agents_app.db.thread_repository import BindingConflict, ThreadRepository
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
    migrate()
    with connect(db.config) as conn:
        assert len(conn.execute("SELECT version FROM schema_migrations").fetchall()) == 8
    with pytest.raises(RuntimeError), connect(db.config) as conn:
        conn.execute("INSERT INTO principals(principal_id) VALUES ('00000000-0000-4000-8000-000000000099')")
        raise RuntimeError("rollback")
    with connect(db.config) as conn:
        assert not conn.execute("SELECT * FROM principals").fetchall()


def test_phase_five_rows_survive_destructive_migration_008(db):
    from managed_agents_app.operations import MIGRATIONS, _statements

    schema = f"phase6_{uuid.uuid4().hex}"
    legacy = (
        "agent_sessions",
        "surface_bindings",
        "ingress_events",
        "projection_events",
        "agent_feedback",
        "slack_response_streams",
    )
    with connect(db.config) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        try:
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name.startswith("008_"):
                    break
                for statement in _statements(path.read_text()):
                    conn.execute(statement)
            principal = str(uuid.uuid4())
            thread = str(uuid.uuid4())
            conn.execute("INSERT INTO principals (principal_id) VALUES (%s)", (principal,))
            conn.execute(
                "INSERT INTO agent_sessions (session_id, principal_id, agent_id, environment_id, "
                "created_by_surface) VALUES ('sesn_old', %s, 'old', 'old', 'web')",
                (principal,),
            )
            conn.execute(
                "INSERT INTO surface_bindings (binding_id, session_id, surface, tenant_id, "
                "external_thread_id) VALUES (%s, 'sesn_old', 'slack', 'T', 'C:1')",
                (str(uuid.uuid4()),),
            )
            conn.execute(
                "INSERT INTO agent_threads (thread_id, principal_id, agent_id, context_id, context_state) "
                "VALUES (%s, %s, 'cma', 'context-1', 'ready')",
                (thread, principal),
            )
            conn.execute(
                "INSERT INTO cma_contexts (context_id, creation_message_id, cma_session_id, "
                "lifecycle_state) VALUES ('context-1', 'message-1', 'sesn_current', 'ready')"
            )
            conn.execute(
                "INSERT INTO cma_tasks (task_id, context_id, message_id, sequence, a2a_state, "
                "internal_state) VALUES ('task-1', 'context-1', 'message-1', 1, 'WORKING', 'running')"
            )
            conn.execute(
                "INSERT INTO agent_tasks (task_binding_id, thread_id, agent_id, client_message_id, "
                "task_id, submission_status) VALUES (%s, %s, 'cma', 'web:1', 'task-1', 'bound')",
                (str(uuid.uuid4()), thread),
            )
            migration = MIGRATIONS / "008_remove_legacy_application_sessions.sql"
            for statement in _statements(migration.read_text()):
                conn.execute(statement)
            for statement in _statements(migration.read_text()):
                conn.execute(statement)
            remaining = {
                row["tablename"]
                for row in conn.execute(
                    "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = %s", (schema,)
                )
            }
            assert not remaining.intersection(legacy)
            assert {"principals", "agent_threads", "agent_tasks", "cma_contexts", "cma_tasks"} <= remaining
            assert conn.execute("SELECT task_id FROM agent_tasks").fetchone()["task_id"] == "task-1"
            assert (
                conn.execute("SELECT cma_session_id FROM cma_contexts").fetchone()["cma_session_id"]
                == "sesn_current"
            )
        finally:
            conn.execute("RESET search_path")
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_web_thread_creation_is_idempotent_under_concurrent_retries(db):
    principal = db.config.dev_principal_id
    db.ensure_principal(principal)
    repository = ThreadRepository(db.config)
    request_id = str(uuid.uuid4())
    barrier = Barrier(2)

    def create():
        barrier.wait(timeout=3)
        return repository.create_thread_idempotent(principal, "cma", request_id, "A thread")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create) for _ in range(2)]
        first, second = (future.result() for future in futures)
    assert first["thread_id"] == second["thread_id"]
    assert first["title"] == "A thread"
    with pytest.raises(BindingConflict):
        repository.create_thread_idempotent(principal, "another-agent", request_id)
    with connect(db.config) as conn:
        rows = conn.execute(
            "SELECT thread_id FROM agent_threads WHERE principal_id = %s AND creation_request_id = %s",
            (principal, request_id),
        ).fetchall()
    assert len(rows) == 1


def _new_thread(db: Database, agent_id: str = "cma") -> dict:
    principal_id = str(uuid.uuid4())
    db.ensure_principal(principal_id)
    return ThreadRepository(db.config).create_thread(principal_id, agent_id)


def test_thread_identity_multi_surface_and_logical_send(db):
    repo = ThreadRepository(db.config)
    thread = _new_thread(db)
    thread_id = str(thread["thread_id"])
    assert uuid.UUID(thread_id)
    assert thread["context_id"] is None
    assert thread["context_state"] == "uninitialized"
    assert repo.get_thread(thread_id)["agent_id"] == "cma"
    assert len(repo.list_threads(str(thread["principal_id"]))) == 1

    web = repo.bind_surface(thread_id, "web", "development", f"web:{thread_id}")
    slack = repo.bind_surface(thread_id, "slack", "T001", f"C001:{thread_id}")
    assert {row["binding_id"] for row in repo.list_surface_bindings(thread_id)} == {
        web["binding_id"],
        slack["binding_id"],
    }
    assert (
        repo.get_surface_binding("web", "development", f"web:{thread_id}")["thread_id"] == thread["thread_id"]
    )
    with pytest.raises(BindingConflict):
        repo.bind_surface(str(_new_thread(db)["thread_id"]), "web", "development", f"web:{thread_id}")

    send = repo.record_logical_send(thread_id, "cma", f"web:{uuid.uuid4()}")
    assert send["task_id"] is None and send["submission_status"] == "pending"
    assert (
        repo.record_logical_send(thread_id, "cma", send["client_message_id"])["task_binding_id"]
        == send["task_binding_id"]
    )
    with pytest.raises(BindingConflict):
        repo.record_logical_send(thread_id, "other", send["client_message_id"])
    bound = repo.bind_task_id(thread_id, send["client_message_id"], "task_one")
    assert bound["submission_status"] == "bound"
    assert (
        repo.bind_task_id(thread_id, send["client_message_id"], "task_one")["task_binding_id"]
        == send["task_binding_id"]
    )
    assert repo.get_task_binding("cma", "task_one")["thread_id"] == thread["thread_id"]
    assert repo.get_task_binding_by_message(thread_id, send["client_message_id"])["task_id"] == "task_one"
    with pytest.raises(BindingConflict):
        repo.bind_task_id(thread_id, send["client_message_id"], "task_two")

    assert repo.archive_thread("00000000-0000-4000-8000-000000000099", thread_id) is None
    assert repo.archive_thread(str(thread["principal_id"]), thread_id)["archived_at"] is not None
    assert repo.list_threads(str(thread["principal_id"])) == []
    assert repo.restore_thread(str(thread["principal_id"]), thread_id)["archived_at"] is None


def test_context_claim_single_winner_lease_recovery_and_fencing(db):
    repo = ThreadRepository(db.config)
    thread_id = str(_new_thread(db)["thread_id"])
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        return repo.claim_context_initialization(thread_id, 60)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(claim) for _ in range(2)]]
    assert sorted(result.status for result in results) == ["acquired", "busy"]
    old_claim = next(result.claim_id for result in results if result.status == "acquired")
    assert repo.claim_context_initialization(thread_id, 60).status == "busy"
    with connect(db.config) as conn:
        conn.execute(
            "UPDATE agent_threads SET context_init_lease_expires_at = "
            "CURRENT_TIMESTAMP - INTERVAL '1 second' "
            "WHERE thread_id = %s",
            (thread_id,),
        )
    recovered = repo.claim_context_initialization(thread_id, 60)
    assert recovered.status == "acquired" and recovered.claim_id != old_claim
    assert not repo.release_context_initialization(thread_id, old_claim)
    with pytest.raises(BindingConflict):
        repo.bind_context(thread_id, old_claim, "ctx_stale")
    bound = repo.bind_context(thread_id, recovered.claim_id, "ctx_ready")
    assert bound["context_state"] == "ready"
    same = repo.bind_context(thread_id, recovered.claim_id, "ctx_ready")
    assert same["thread_id"] == bound["thread_id"]
    assert same["context_id"] == "ctx_ready"
    assert repo.claim_context_initialization(thread_id, 60).context_id == "ctx_ready"
    with pytest.raises(BindingConflict):
        repo.bind_context(thread_id, recovered.claim_id, "ctx_other")

    same_agent = str(_new_thread(db)["thread_id"])
    claim_id = repo.claim_context_initialization(same_agent, 60).claim_id
    with pytest.raises(BindingConflict):
        repo.bind_context(same_agent, claim_id, "ctx_ready")
    other_agent = str(_new_thread(db, "other")["thread_id"])
    other_claim = repo.claim_context_initialization(other_agent, 60)
    assert repo.bind_context(other_agent, other_claim.claim_id, "ctx_ready")["context_id"] == "ctx_ready"


def test_surface_and_logical_send_creation_races(db):
    repo = ThreadRepository(db.config)
    thread_id = str(_new_thread(db)["thread_id"])
    barrier = Barrier(2)
    external = f"C001:{uuid.uuid4()}"

    def bind():
        barrier.wait(timeout=5)
        return repo.bind_surface(thread_id, "slack", "T001", external)

    with ThreadPoolExecutor(max_workers=2) as pool:
        bindings = [future.result() for future in [pool.submit(bind) for _ in range(2)]]
    assert bindings[0]["binding_id"] == bindings[1]["binding_id"]

    competing_thread = str(_new_thread(db)["thread_id"])
    contested_external = f"C001:{uuid.uuid4()}"
    barrier = Barrier(2)

    def contested_bind(candidate: str):
        barrier.wait(timeout=5)
        try:
            return repo.bind_surface(candidate, "slack", "T001", contested_external)
        except BindingConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        contested = [
            future.result()
            for future in [
                pool.submit(contested_bind, thread_id),
                pool.submit(contested_bind, competing_thread),
            ]
        ]
    assert sum(item is not None for item in contested) == 1
    assert repo.get_surface_binding("slack", "T001", contested_external)["thread_id"] in {
        uuid.UUID(thread_id),
        uuid.UUID(competing_thread),
    }

    barrier = Barrier(2)
    message_id = f"web:{uuid.uuid4()}"

    def send():
        barrier.wait(timeout=5)
        return repo.record_logical_send(thread_id, "cma", message_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        sends = [future.result() for future in [pool.submit(send) for _ in range(2)]]
    assert sends[0]["task_binding_id"] == sends[1]["task_binding_id"]
    repo.bind_task_id(thread_id, message_id, "task_shared")
    other = repo.record_logical_send(thread_id, "cma", f"web:{uuid.uuid4()}")
    with pytest.raises(BindingConflict):
        repo.bind_task_id(thread_id, other["client_message_id"], "task_shared")


def test_cma_first_message_idempotency_and_private_task_identity(db):
    repo = CmaControllerRepository(db.config)
    barrier = Barrier(2)
    message_id = f"creation:{uuid.uuid4()}"

    def create(index):
        barrier.wait(timeout=5)
        return repo.create_initializing_context(f"ctx_{index}_{uuid.uuid4()}", message_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        contexts = [future.result() for future in [pool.submit(create, i) for i in range(2)]]
    assert contexts[0]["context_id"] == contexts[1]["context_id"]
    context_id = contexts[0]["context_id"]
    assert repo.find_context_by_creation_message_id(message_id)["context_id"] == context_id
    assert repo.bind_cma_session(context_id, "cma_session_1")["lifecycle_state"] == "ready"
    assert repo.bind_cma_session(context_id, "cma_session_1")["cma_session_id"] == "cma_session_1"
    with pytest.raises(ControllerIdentityConflict):
        repo.bind_cma_session(context_id, "cma_session_2")
    assert repo.get_context(context_id)["cma_session_id"] == "cma_session_1"

    task = repo.create_task("task_private", context_id, "message_1", 1, "TASK_STATE_SUBMITTED", "queued")
    assert (
        repo.create_task("task_duplicate", context_id, "message_1", 2, "TASK_STATE_SUBMITTED", "queued")[
            "task_id"
        ]
        == task["task_id"]
    )
    assert repo.find_task_by_message_id(context_id, "message_1")["task_id"] == task["task_id"]
    assert repo.get_task("task_private")["sequence"] == 1
    with pytest.raises(ControllerIdentityConflict):
        repo.create_task("task_second", context_id, "message_2", 1, "TASK_STATE_SUBMITTED", "queued")

    with pytest.raises(ControllerIdentityConflict):
        repo.create_initializing_context(context_id, f"creation:{uuid.uuid4()}")
    assert repo.get_context(context_id)["creation_message_id"] == message_id

    with pytest.raises(ControllerIdentityConflict):
        repo.create_task("task_private", context_id, "message_2", 2, "TASK_STATE_SUBMITTED", "queued")
    assert repo.get_task("task_private")["message_id"] == "message_1"

    second_context = repo.create_initializing_context(f"ctx_{uuid.uuid4()}", f"creation:{uuid.uuid4()}")
    with pytest.raises(ControllerIdentityConflict):
        repo.create_task(
            "task_private", second_context["context_id"], "message_1", 1, "TASK_STATE_SUBMITTED", "queued"
        )
    assert repo.get_task("task_private")["context_id"] == context_id


@pytest.mark.parametrize(
    "state,context_id,claim_id,has_lease",
    [
        ("ready", None, None, False),
        ("initializing", None, None, True),
        ("initializing", None, "00000000-0000-4000-8000-000000000001", False),
        ("uninitialized", "ctx_invalid", None, False),
        ("ready", "ctx_invalid", "00000000-0000-4000-8000-000000000001", False),
    ],
)
def test_thread_lifecycle_check_rejects_impossible_state(db, state, context_id, claim_id, has_lease):
    thread_id = str(_new_thread(db)["thread_id"])
    with pytest.raises(CheckViolation), connect(db.config) as conn:
        conn.execute(
            "UPDATE agent_threads SET context_state = %s, context_id = %s, context_init_claim_id = %s, "
            "context_init_lease_expires_at = "
            "CASE WHEN %s THEN CURRENT_TIMESTAMP + INTERVAL '1 minute' ELSE NULL END "
            "WHERE thread_id = %s",
            (state, context_id, claim_id, has_lease, thread_id),
        )


@pytest.mark.parametrize(
    "state,session_id,next_sequence",
    [("ready", None, 1), ("initializing", "cma_session_invalid", 1), ("initializing", None, 0)],
)
def test_cma_context_lifecycle_check_rejects_impossible_state(db, state, session_id, next_sequence):
    repo = CmaControllerRepository(db.config)
    context = repo.create_initializing_context(f"ctx_{uuid.uuid4()}", f"creation:{uuid.uuid4()}")
    with pytest.raises(CheckViolation), connect(db.config) as conn:
        conn.execute(
            "UPDATE cma_contexts SET lifecycle_state = %s, cma_session_id = %s, "
            "next_sequence = %s WHERE context_id = %s",
            (state, session_id, next_sequence, context["context_id"]),
        )


def test_cma_task_sequence_check_rejects_zero(db):
    repo = CmaControllerRepository(db.config)
    context = repo.create_initializing_context(f"ctx_{uuid.uuid4()}", f"creation:{uuid.uuid4()}")
    task = repo.create_task(
        f"task_{uuid.uuid4()}", context["context_id"], "message_1", 1, "TASK_STATE_SUBMITTED", "queued"
    )
    with pytest.raises(CheckViolation), connect(db.config) as conn:
        conn.execute("UPDATE cma_tasks SET sequence = 0 WHERE task_id = %s", (task["task_id"],))


@pytest.mark.parametrize(
    "status,task_id",
    [("pending", "task_invalid"), ("bound", None)],
)
def test_logical_send_check_rejects_impossible_state(db, status, task_id):
    repo = ThreadRepository(db.config)
    thread_id = str(_new_thread(db)["thread_id"])
    send = repo.record_logical_send(thread_id, "cma", f"web:{uuid.uuid4()}")
    with pytest.raises(CheckViolation), connect(db.config) as conn:
        conn.execute(
            "UPDATE agent_tasks SET submission_status = %s, task_id = %s WHERE task_binding_id = %s",
            (status, task_id, send["task_binding_id"]),
        )
