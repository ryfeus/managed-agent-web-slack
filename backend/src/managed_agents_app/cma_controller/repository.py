"""CMA context and task identity persistence; no application identity is accepted."""

from __future__ import annotations

from typing import Any

from psycopg.errors import UniqueViolation

from managed_agents_app.config import AppConfig
from managed_agents_app.db.connection import connect, with_dsql_retry


class ControllerIdentityConflict(ValueError):
    """A controller identity is already bound to a different object."""


class CmaControllerRepository:
    def __init__(self, config: AppConfig, role: str | None = None) -> None:
        self.config = config
        self.role = role

    def create_initializing_context(self, context_id: str, creation_message_id: str) -> dict[str, Any]:
        if not context_id or not creation_message_id:
            raise ValueError("Context and creation message IDs are required")

        def operation() -> dict[str, Any]:
            try:
                with connect(self.config, self.role) as conn, conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO cma_contexts (context_id, creation_message_id, lifecycle_state) "
                        "VALUES (%s, %s, 'initializing') "
                        "ON CONFLICT (creation_message_id) "
                        "DO UPDATE SET creation_message_id = EXCLUDED.creation_message_id RETURNING *",
                        (context_id, creation_message_id),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise RuntimeError("Context creation returned no row")
                    return row
            except UniqueViolation as error:
                raise ControllerIdentityConflict("Context ID belongs to another creation message") from error

        return with_dsql_retry(operation)

    def find_context_by_creation_message_id(self, message_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM cma_contexts WHERE creation_message_id = %s", (message_id,))
            return cur.fetchone()

    def bind_cma_session(self, context_id: str, cma_session_id: str) -> dict[str, Any]:
        if not cma_session_id:
            raise ValueError("CMA session ID is required")

        def operation() -> dict[str, Any]:
            try:
                with connect(self.config, self.role) as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE cma_contexts SET cma_session_id = %s, lifecycle_state = 'ready', "
                        "updated_at = CURRENT_TIMESTAMP WHERE context_id = %s AND cma_session_id IS NULL "
                        "RETURNING *",
                        (cma_session_id, context_id),
                    )
                    if row := cur.fetchone():
                        return row
                    cur.execute("SELECT * FROM cma_contexts WHERE context_id = %s", (context_id,))
                    row = cur.fetchone()
                    if row is None:
                        raise KeyError("Unknown CMA context")
                    if row["cma_session_id"] == cma_session_id and row["lifecycle_state"] == "ready":
                        return row
                    raise ControllerIdentityConflict("Context is bound to another CMA session")
            except UniqueViolation as error:
                raise ControllerIdentityConflict("CMA session is bound to another context") from error

        return with_dsql_retry(operation)

    def get_context(self, context_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM cma_contexts WHERE context_id = %s", (context_id,))
            return cur.fetchone()

    def create_task(
        self,
        task_id: str,
        context_id: str,
        message_id: str,
        sequence: int,
        a2a_state: str,
        internal_state: str,
    ) -> dict[str, Any]:
        if not task_id or not context_id or not message_id or sequence < 1:
            raise ValueError("Valid task, context, message IDs, and sequence are required")

        def operation() -> dict[str, Any]:
            try:
                with connect(self.config, self.role) as conn, conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO cma_tasks "
                        "(task_id, context_id, message_id, sequence, a2a_state, internal_state) "
                        "VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (context_id, message_id) "
                        "DO UPDATE SET message_id = EXCLUDED.message_id RETURNING *",
                        (task_id, context_id, message_id, sequence, a2a_state, internal_state),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise RuntimeError("Task creation returned no row")
                    return row
            except UniqueViolation as error:
                raise ControllerIdentityConflict("Task ID or context sequence is already assigned") from error

        return with_dsql_retry(operation)

    def find_task_by_message_id(self, context_id: str, message_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cma_tasks WHERE context_id = %s AND message_id = %s",
                (context_id, message_id),
            )
            return cur.fetchone()

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM cma_tasks WHERE task_id = %s", (task_id,))
            return cur.fetchone()
