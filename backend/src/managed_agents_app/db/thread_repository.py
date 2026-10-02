"""Application-owned thread, surface, and logical-send persistence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from psycopg.errors import UniqueViolation

from managed_agents_app.config import AppConfig
from managed_agents_app.db.connection import connect, with_dsql_retry


class BindingConflict(ValueError):
    """A stable identity is already bound to a different owner or target."""


class _SurfaceCreationRace(RuntimeError):
    pass


@dataclass(frozen=True)
class ContextClaim:
    status: Literal["acquired", "busy", "ready"]
    claim_id: str | None = None
    context_id: str | None = None


class ThreadRepository:
    def __init__(self, config: AppConfig, role: str | None = None) -> None:
        self.config = config
        self.role = role

    def create_thread(self, principal_id: str, agent_id: str, title: str | None = None) -> dict[str, Any]:
        if not agent_id:
            raise ValueError("agent_id is required")
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_threads (thread_id, principal_id, agent_id, title) "
                "VALUES (%s, %s, %s, %s) RETURNING *",
                (str(uuid.uuid4()), principal_id, agent_id, title),
            )
            return self._row(cur.fetchone())

    def create_thread_idempotent(
        self, principal_id: str, agent_id: str, creation_request_id: str, title: str | None = None
    ) -> dict[str, Any]:
        if not agent_id or not creation_request_id:
            raise ValueError("Agent and creation request ID are required")

        def operation() -> dict[str, Any]:
            with connect(self.config, self.role) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_threads "
                    "(thread_id, principal_id, agent_id, creation_request_id, title) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (principal_id, creation_request_id) "
                    "DO UPDATE SET creation_request_id = EXCLUDED.creation_request_id RETURNING *",
                    (str(uuid.uuid4()), principal_id, agent_id, creation_request_id, title),
                )
                row = self._row(cur.fetchone())
                if row["agent_id"] != agent_id:
                    raise BindingConflict("Creation request is bound to another agent")
                return row

        return with_dsql_retry(operation)

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM agent_threads WHERE thread_id = %s", (thread_id,))
            return cur.fetchone()

    def get_owned_thread(self, principal_id: str, thread_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM agent_threads WHERE thread_id = %s AND principal_id = %s",
                (thread_id, principal_id),
            )
            return cur.fetchone()

    def list_threads(self, principal_id: str) -> list[dict[str, Any]]:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM agent_threads WHERE principal_id = %s AND archived_at IS NULL "
                "ORDER BY updated_at DESC, thread_id DESC",
                (principal_id,),
            )
            return list(cur.fetchall())

    def update_title_if_empty(self, thread_id: str, title: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_threads SET title = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE thread_id = %s AND (title IS NULL OR title = '') RETURNING *",
                (title[:60], thread_id),
            )
            return cur.fetchone()

    def archive_thread(self, principal_id: str, thread_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_threads SET archived_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE principal_id = %s AND thread_id = %s AND archived_at IS NULL RETURNING *",
                (principal_id, thread_id),
            )
            return cur.fetchone()

    def restore_thread(self, principal_id: str, thread_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_threads SET archived_at = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE principal_id = %s AND thread_id = %s AND archived_at IS NOT NULL RETURNING *",
                (principal_id, thread_id),
            )
            return cur.fetchone()

    def get_surface_binding(
        self, surface: str, tenant_id: str, external_thread_id: str
    ) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM thread_surface_bindings WHERE surface = %s AND tenant_id = %s "
                "AND external_thread_id = %s",
                (surface, tenant_id, external_thread_id),
            )
            return cur.fetchone()

    def get_or_create_surface_thread(
        self,
        principal_id: str,
        agent_id: str,
        surface: str,
        tenant_id: str,
        external_thread_id: str,
        title: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Create the thread and binding in one transaction; losing creators roll back."""

        def operation() -> tuple[dict[str, Any], dict[str, Any]]:
            with connect(self.config, self.role) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT b.*, t.principal_id, t.agent_id, t.archived_at FROM thread_surface_bindings b "
                    "JOIN agent_threads t ON t.thread_id = b.thread_id "
                    "WHERE b.surface = %s AND b.tenant_id = %s AND b.external_thread_id = %s",
                    (surface, tenant_id, external_thread_id),
                )
                existing = cur.fetchone()
                if existing:
                    self._check_binding(existing, principal_id, agent_id)
                    cur.execute("SELECT * FROM agent_threads WHERE thread_id = %s", (existing["thread_id"],))
                    return self._row(cur.fetchone()), existing
                thread_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO agent_threads (thread_id, principal_id, agent_id, title) "
                    "VALUES (%s, %s, %s, %s) RETURNING *",
                    (thread_id, principal_id, agent_id, title),
                )
                thread = self._row(cur.fetchone())
                cur.execute(
                    "INSERT INTO thread_surface_bindings "
                    "(binding_id, thread_id, surface, tenant_id, external_thread_id) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (surface, tenant_id, external_thread_id) DO NOTHING RETURNING *",
                    (str(uuid.uuid4()), thread_id, surface, tenant_id, external_thread_id),
                )
                binding = cur.fetchone()
                if binding is None:
                    raise _SurfaceCreationRace()
                return thread, binding

        try:
            return with_dsql_retry(operation)
        except _SurfaceCreationRace:
            # The transaction containing the newly created thread was rolled back.
            binding = self.get_surface_binding(surface, tenant_id, external_thread_id)
            if binding is None:
                return self.get_or_create_surface_thread(
                    principal_id, agent_id, surface, tenant_id, external_thread_id, title
                )
            thread = self.get_thread(str(binding["thread_id"]))
            if thread is None:
                raise RuntimeError("Surface binding has no thread") from None
            self._check_binding(thread, principal_id, agent_id)
            return thread, binding

    @staticmethod
    def _check_binding(row: dict[str, Any], principal_id: str, agent_id: str) -> None:
        if str(row["principal_id"]) != principal_id or str(row["agent_id"]) != agent_id:
            raise BindingConflict("Surface thread belongs to another principal or agent")
        if row["archived_at"] is not None:
            raise BindingConflict("Surface thread is archived")

    def owns_thread(self, principal_id: str, thread_id: str) -> bool:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM agent_threads WHERE thread_id = %s AND principal_id = %s "
                "AND archived_at IS NULL",
                (thread_id, principal_id),
            )
            return cur.fetchone() is not None

    def get_latest_task_binding(self, thread_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM agent_tasks WHERE thread_id = %s AND task_id IS NOT NULL "
                "ORDER BY created_at DESC, task_binding_id DESC LIMIT 1",
                (thread_id,),
            )
            return cur.fetchone()

    def list_task_bindings(self, thread_id: str) -> list[dict[str, Any]]:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM agent_tasks WHERE thread_id = %s ORDER BY created_at, task_binding_id",
                (thread_id,),
            )
            return list(cur.fetchall())

    def bind_surface(
        self, thread_id: str, surface: str, tenant_id: str, external_thread_id: str
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with connect(self.config, self.role) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO thread_surface_bindings "
                    "(binding_id, thread_id, surface, tenant_id, external_thread_id) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (surface, tenant_id, external_thread_id) "
                    "DO UPDATE SET external_thread_id = EXCLUDED.external_thread_id RETURNING *",
                    (str(uuid.uuid4()), thread_id, surface, tenant_id, external_thread_id),
                )
                row = self._row(cur.fetchone())
                if str(row["thread_id"]) != thread_id:
                    raise BindingConflict("Surface is already bound to another thread")
                return row

        return with_dsql_retry(operation)

    def list_surface_bindings(self, thread_id: str) -> list[dict[str, Any]]:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM thread_surface_bindings WHERE thread_id = %s ORDER BY created_at, binding_id",
                (thread_id,),
            )
            return list(cur.fetchall())

    def record_logical_send(self, thread_id: str, agent_id: str, client_message_id: str) -> dict[str, Any]:
        if not client_message_id:
            raise ValueError("client_message_id is required")

        def operation() -> dict[str, Any]:
            with connect(self.config, self.role) as conn, conn.cursor() as cur:
                cur.execute("SELECT agent_id FROM agent_threads WHERE thread_id = %s", (thread_id,))
                thread = cur.fetchone()
                if not thread:
                    raise KeyError("Unknown thread")
                if thread["agent_id"] != agent_id:
                    raise BindingConflict("Agent does not match thread")
                cur.execute(
                    "INSERT INTO agent_tasks "
                    "(task_binding_id, thread_id, agent_id, client_message_id, submission_status) "
                    "VALUES (%s, %s, %s, %s, 'pending') "
                    "ON CONFLICT (thread_id, client_message_id) "
                    "DO UPDATE SET client_message_id = EXCLUDED.client_message_id RETURNING *",
                    (str(uuid.uuid4()), thread_id, agent_id, client_message_id),
                )
                row = self._row(cur.fetchone())
                if row["agent_id"] != agent_id:
                    raise BindingConflict("Logical send is bound to another agent")
                cur.execute(
                    "UPDATE agent_threads SET updated_at = CURRENT_TIMESTAMP WHERE thread_id = %s",
                    (thread_id,),
                )
                return row

        return with_dsql_retry(operation)

    def bind_task_id(self, thread_id: str, client_message_id: str, task_id: str) -> dict[str, Any]:
        if not task_id:
            raise ValueError("task_id is required")

        def operation() -> dict[str, Any]:
            try:
                with connect(self.config, self.role) as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE agent_tasks SET task_id = %s, submission_status = 'bound', "
                        "updated_at = CURRENT_TIMESTAMP WHERE thread_id = %s AND client_message_id = %s "
                        "AND submission_status = 'pending' AND task_id IS NULL RETURNING *",
                        (task_id, thread_id, client_message_id),
                    )
                    if row := cur.fetchone():
                        return row
                    cur.execute(
                        "SELECT * FROM agent_tasks WHERE thread_id = %s AND client_message_id = %s",
                        (thread_id, client_message_id),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise KeyError("Unknown logical send")
                    if row["task_id"] == task_id and row["submission_status"] == "bound":
                        return row
                    raise BindingConflict("Logical send is not pending or is bound to another task")
            except UniqueViolation as error:
                raise BindingConflict("Task ID is already bound to another logical send") from error

        return with_dsql_retry(operation)

    def get_task_binding(self, agent_id: str, task_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM agent_tasks WHERE agent_id = %s AND task_id = %s", (agent_id, task_id))
            return cur.fetchone()

    def get_task_binding_by_message(self, thread_id: str, client_message_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM agent_tasks WHERE thread_id = %s AND client_message_id = %s",
                (thread_id, client_message_id),
            )
            return cur.fetchone()

    def claim_context_initialization(self, thread_id: str, lease_duration_seconds: int) -> ContextClaim:
        if lease_duration_seconds <= 0:
            raise ValueError("Lease duration must be positive")
        claim_id = str(uuid.uuid4())

        def operation() -> ContextClaim:
            with connect(self.config, self.role) as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_threads SET context_state = 'initializing', context_init_claim_id = %s, "
                    "context_init_lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'), "
                    "updated_at = CURRENT_TIMESTAMP WHERE thread_id = %s AND "
                    "(context_state = 'uninitialized' OR (context_state = 'initializing' "
                    "AND (context_init_lease_expires_at IS NULL OR "
                    "context_init_lease_expires_at <= CURRENT_TIMESTAMP))) RETURNING thread_id",
                    (claim_id, lease_duration_seconds, thread_id),
                )
                if cur.fetchone():
                    return ContextClaim("acquired", claim_id=claim_id)
                cur.execute(
                    "SELECT context_state, context_id FROM agent_threads WHERE thread_id = %s",
                    (thread_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise KeyError("Unknown thread")
                if row["context_state"] == "ready":
                    return ContextClaim("ready", context_id=str(row["context_id"]))
                return ContextClaim("busy")

        return with_dsql_retry(operation)

    def bind_context(self, thread_id: str, claim_id: str, context_id: str) -> dict[str, Any]:
        if not context_id:
            raise ValueError("context_id is required")

        def operation() -> dict[str, Any]:
            try:
                with connect(self.config, self.role) as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE agent_threads SET context_id = %s, context_state = 'ready', "
                        "context_init_claim_id = NULL, context_init_lease_expires_at = NULL, "
                        "updated_at = CURRENT_TIMESTAMP WHERE thread_id = %s "
                        "AND context_state = 'initializing' AND context_init_claim_id = %s "
                        "AND context_init_lease_expires_at > CURRENT_TIMESTAMP RETURNING *",
                        (context_id, thread_id, claim_id),
                    )
                    if row := cur.fetchone():
                        return row
                    cur.execute("SELECT * FROM agent_threads WHERE thread_id = %s", (thread_id,))
                    row = cur.fetchone()
                    if row is None:
                        raise KeyError("Unknown thread")
                    if row["context_state"] == "ready" and row["context_id"] == context_id:
                        return row
                    raise BindingConflict("Context claim is stale or thread is bound to another context")
            except UniqueViolation as error:
                raise BindingConflict("Context is already bound to another thread for this agent") from error

        return with_dsql_retry(operation)

    def release_context_initialization(self, thread_id: str, claim_id: str) -> bool:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_threads SET context_state = 'uninitialized', context_init_claim_id = NULL, "
                "context_init_lease_expires_at = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE thread_id = %s AND context_state = 'initializing' "
                "AND context_init_claim_id = %s RETURNING thread_id",
                (thread_id, claim_id),
            )
            return cur.fetchone() is not None

    @staticmethod
    def _row(row: dict[str, Any] | None) -> dict[str, Any]:
        if row is None:
            raise RuntimeError("Expected a database row")
        return row
