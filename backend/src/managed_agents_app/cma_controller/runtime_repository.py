"""Durable, controller-private ordering and scheduler state.

No application thread, principal, surface, or message content enters this module.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from psycopg.errors import UniqueViolation

from managed_agents_app.cma_controller.repository import CmaControllerRepository
from managed_agents_app.db.connection import connect, with_dsql_retry


class LostSchedulerClaim(RuntimeError):
    """A worker's lease was replaced or expired."""


class ControllerRuntimeRepository(CmaControllerRepository):
    def admit(
        self,
        *,
        proposed_context_id: str,
        requested_context_id: str | None,
        message_id: str,
        proposed_task_id: str,
        input_object_key: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Atomically create a context/turn or recover an existing message ID."""
        if not all((proposed_context_id, message_id, proposed_task_id, input_object_key)):
            raise ValueError("Context, message, task, and input key are required")

        def operation() -> tuple[dict[str, Any], dict[str, Any], bool]:
            with connect(self.config, self.role) as conn, conn.cursor() as cur:
                if requested_context_id is None:
                    cur.execute(
                        "INSERT INTO cma_contexts (context_id, creation_message_id, lifecycle_state) "
                        "VALUES (%s, %s, 'initializing') ON CONFLICT (creation_message_id) "
                        "DO UPDATE SET creation_message_id = EXCLUDED.creation_message_id RETURNING *",
                        (proposed_context_id, message_id),
                    )
                    context = cur.fetchone()
                else:
                    cur.execute("SELECT * FROM cma_contexts WHERE context_id = %s", (requested_context_id,))
                    context = cur.fetchone()
                if context is None:
                    raise KeyError("Unknown CMA context")
                context_id = str(context["context_id"])
                cur.execute(
                    "SELECT * FROM cma_tasks WHERE context_id = %s AND message_id = %s",
                    (context_id, message_id),
                )
                if existing := cur.fetchone():
                    return context, existing, False

                cur.execute(
                    "UPDATE cma_contexts SET next_sequence = next_sequence + 1, "
                    "updated_at = CURRENT_TIMESTAMP WHERE context_id = %s RETURNING *",
                    (context_id,),
                )
                context = cur.fetchone()
                if context is None:
                    raise KeyError("Unknown CMA context")
                rejected = context["lifecycle_state"] == "failed"
                rejection_reason = "context-failed" if rejected else None
                if context["active_task_id"]:
                    cur.execute(
                        "SELECT internal_state FROM cma_tasks WHERE task_id = %s FOR UPDATE",
                        (context["active_task_id"],),
                    )
                    active = cur.fetchone()
                    if active is not None and active["internal_state"] == "input_required":
                        rejected = True
                        rejection_reason = "input-required"
                cur.execute(
                    "INSERT INTO cma_tasks (task_id, context_id, message_id, sequence, "
                    "a2a_state, internal_state, input_object_key, held_reason, dispatch_attempts) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0) "
                    "RETURNING *",
                    (
                        proposed_task_id,
                        context_id,
                        message_id,
                        cast(int, context["next_sequence"]) - 1,
                        "REJECTED" if rejected else "SUBMITTED",
                        "terminal" if rejected else "queued",
                        input_object_key,
                        rejection_reason,
                    ),
                )
                task = cur.fetchone()
                assert task is not None
                return context, task, True

        try:
            return with_dsql_retry(operation)
        except UniqueViolation:
            context = (
                self.find_context_by_creation_message_id(message_id)
                if requested_context_id is None
                else self.get_context(requested_context_id)
            )
            if context is None:
                raise
            task = self.find_task_by_message_id(str(context["context_id"]), message_id)
            if task is None:
                raise
            return context, task, False

    def context_for_session(self, session_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM cma_contexts WHERE cma_session_id = %s", (session_id,))
            return cur.fetchone()

    def claim_scheduler(self, context_id: str, lease_seconds: int = 60) -> str | None:
        claim_id = str(uuid.uuid4())

        def operation() -> str | None:
            with connect(self.config, self.role) as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE cma_contexts SET scheduler_claim_id = %s, "
                    "scheduler_lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second') "
                    "WHERE context_id = %s AND (scheduler_claim_id IS NULL OR "
                    "scheduler_lease_expires_at <= CURRENT_TIMESTAMP) RETURNING context_id",
                    (claim_id, lease_seconds, context_id),
                )
                return claim_id if cur.fetchone() else None

        return with_dsql_retry(operation)

    def release_scheduler(self, context_id: str, claim_id: str) -> bool:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE cma_contexts SET scheduler_claim_id = NULL, "
                "scheduler_lease_expires_at = NULL WHERE context_id = %s "
                "AND scheduler_claim_id = %s RETURNING context_id",
                (context_id, claim_id),
            )
            return cur.fetchone() is not None

    def _fenced_update(
        self, table: str, key: str, key_value: str, context_id: str, claim_id: str, **fields: Any
    ) -> dict[str, Any]:
        allowed = {
            "cma_contexts": {"cma_session_id", "lifecycle_state", "active_task_id"},
            "cma_tasks": {
                "a2a_state",
                "internal_state",
                "cma_input_event_id",
                "input_object_key",
                "cma_predecessor_event_id",
                "cma_terminal_event_id",
                "dispatch_attempts",
                "dispatch_attempted_at",
                "next_dispatch_at",
                "cancel_requested_at",
                "cancel_attempted_at",
                "held_reason",
            },
        }
        if not fields or not set(fields) <= allowed[table]:
            raise ValueError("Unsupported controller update")
        assignments = ", ".join(f"{field} = %s" for field in fields)
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE {table} SET {assignments}, updated_at = CURRENT_TIMESTAMP "
                f"WHERE {key} = %s AND EXISTS (SELECT 1 FROM cma_contexts AS lease "
                "WHERE lease.context_id = %s AND lease.scheduler_claim_id = %s "
                "AND lease.scheduler_lease_expires_at > CURRENT_TIMESTAMP) RETURNING *",
                (*fields.values(), key_value, context_id, claim_id),
            )
            row = cur.fetchone()
            if row is None:
                raise LostSchedulerClaim(context_id)
            return row

    def update_context(self, context_id: str, claim_id: str, **fields: Any) -> dict[str, Any]:
        return self._fenced_update("cma_contexts", "context_id", context_id, context_id, claim_id, **fields)

    def update_task(self, task_id: str, context_id: str, claim_id: str, **fields: Any) -> dict[str, Any]:
        return self._fenced_update("cma_tasks", "task_id", task_id, context_id, claim_id, **fields)

    def list_tasks(self, context_id: str, after_sequence: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cma_tasks WHERE context_id = %s AND sequence > %s ORDER BY sequence LIMIT %s",
                (context_id, after_sequence, limit),
            )
            return list(cur.fetchall())

    def count_tasks(self, context_id: str) -> int:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM cma_tasks WHERE context_id = %s", (context_id,))
            row = cur.fetchone()
            return cast(int, row["total"]) if row else 0

    def pending_input_keys(self) -> set[str]:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute("SELECT input_object_key FROM cma_tasks WHERE input_object_key IS NOT NULL")
            keys = {str(row["input_object_key"]) for row in cur.fetchall()}
            cur.execute(
                "SELECT decision_reason_object_key FROM cma_input_requests "
                "WHERE decision_reason_object_key IS NOT NULL AND status <> 'resolved'"
            )
            keys.update(str(row["decision_reason_object_key"]) for row in cur.fetchall())
            cur.execute(
                "SELECT answer_object_key FROM cma_clarification_requests "
                "WHERE answer_object_key IS NOT NULL AND status <> 'resolved'"
            )
            keys.update(str(row["answer_object_key"]) for row in cur.fetchall())
            return keys

    def stale_pending_input_count(self) -> int:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS total FROM cma_tasks WHERE input_object_key IS NOT NULL "
                "AND internal_state <> 'terminal' "
                "AND created_at < CURRENT_TIMESTAMP - INTERVAL '1 day'"
            )
            row = cur.fetchone()
            return cast(int, row["total"]) if row else 0

    def list_held_tasks(self) -> list[dict[str, Any]]:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT task_id, context_id, a2a_state, internal_state, held_reason, "
                "dispatch_attempts, created_at, updated_at FROM cma_tasks "
                "WHERE held_reason IS NOT NULL AND internal_state <> 'terminal' "
                "ORDER BY created_at, task_id"
            )
            return list(cur.fetchall())

    def resolve_held_task(self, task_id: str, claim_id: str, resolution: str) -> str:
        """Resolve an active held task atomically under the scheduler lease."""
        if resolution not in {"fail", "retry"}:
            raise ValueError("Unsupported held-task resolution")
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT t.*, c.active_task_id, c.cma_session_id FROM cma_tasks t "
                "JOIN cma_contexts c ON c.context_id = t.context_id "
                "WHERE t.task_id = %s AND c.scheduler_claim_id = %s "
                "AND c.scheduler_lease_expires_at > CURRENT_TIMESTAMP FOR UPDATE",
                (task_id, claim_id),
            )
            task = cur.fetchone()
            if (
                task is None
                or task["internal_state"] == "terminal"
                or not task["held_reason"]
                or str(task["active_task_id"]) != task_id
            ):
                raise ValueError("Task is not an active held task")
            context_id = str(task["context_id"])
            if resolution == "retry":
                if (
                    task["held_reason"] not in {"ambiguous-send", "ambiguous-dispatch"}
                    or task["cma_input_event_id"] is not None
                    or task["cma_session_id"] is None
                    or task["cancel_requested_at"] is not None
                ):
                    raise ValueError("This held operation cannot be retried")
                cur.execute(
                    "UPDATE cma_tasks SET internal_state = 'queued', a2a_state = 'SUBMITTED', "
                    "held_reason = NULL, cma_predecessor_event_id = NULL, "
                    "dispatch_attempted_at = NULL, next_dispatch_at = NULL, "
                    "updated_at = CURRENT_TIMESTAMP WHERE task_id = %s",
                    (task_id,),
                )
            else:
                cur.execute(
                    "UPDATE cma_tasks SET internal_state = 'terminal', a2a_state = 'FAILED', "
                    "held_reason = NULL, updated_at = CURRENT_TIMESTAMP WHERE task_id = %s",
                    (task_id,),
                )
            cur.execute(
                "UPDATE cma_contexts SET active_task_id = NULL, "
                "lifecycle_state = CASE WHEN %s = 'fail' AND cma_session_id IS NULL "
                "THEN 'failed' ELSE lifecycle_state END, updated_at = CURRENT_TIMESTAMP "
                "WHERE context_id = %s AND scheduler_claim_id = %s "
                "AND scheduler_lease_expires_at > CURRENT_TIMESTAMP",
                (resolution, context_id, claim_id),
            )
            if cur.rowcount != 1:
                raise LostSchedulerClaim(context_id)
            return context_id

    def next_queued(self, context_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cma_tasks WHERE context_id = %s AND internal_state = 'queued' "
                "ORDER BY sequence LIMIT 1",
                (context_id,),
            )
            return cur.fetchone()

    def activate_task(
        self, context_id: str, task_id: str, claim_id: str, predecessor_event_id: str | None
    ) -> dict[str, Any]:
        """Bind the one active task and its dispatch marker in one transaction."""
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE cma_contexts SET active_task_id = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE context_id = %s AND scheduler_claim_id = %s "
                "AND scheduler_lease_expires_at > CURRENT_TIMESTAMP AND active_task_id IS NULL "
                "RETURNING context_id",
                (task_id, context_id, claim_id),
            )
            if cur.fetchone() is None:
                raise LostSchedulerClaim(context_id)
            cur.execute(
                "UPDATE cma_tasks SET internal_state = 'dispatching', "
                "cma_predecessor_event_id = %s, dispatch_attempts = COALESCE(dispatch_attempts, 0) + 1, "
                "dispatch_attempted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE task_id = %s AND context_id = %s AND internal_state = 'queued' RETURNING *",
                (predecessor_event_id, task_id, context_id),
            )
            row = cur.fetchone()
            if row is None:
                raise LostSchedulerClaim(context_id)
            return row

    def cancel_task(self, task_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE cma_tasks SET a2a_state = 'CANCELED', internal_state = 'terminal', "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE task_id = %s AND internal_state = 'queued' RETURNING *",
                (task_id,),
            )
            if row := cur.fetchone():
                return row
            cur.execute(
                "UPDATE cma_tasks SET cancel_requested_at = "
                "COALESCE(cancel_requested_at, CURRENT_TIMESTAMP), "
                "updated_at = CURRENT_TIMESTAMP WHERE task_id = %s AND internal_state <> 'terminal' "
                "RETURNING *",
                (task_id,),
            )
            if row := cur.fetchone():
                return row
            cur.execute("SELECT * FROM cma_tasks WHERE task_id = %s", (task_id,))
            return cur.fetchone()

    def add_input_request(
        self, request_id: str, task_id: str, cma_event_id: str, context_id: str, claim_id: str
    ) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cma_input_requests (request_id, task_id, cma_event_id, kind, status) "
                "SELECT %s, %s, %s, 'tool_approval', 'pending' FROM cma_contexts "
                "WHERE context_id = %s AND scheduler_claim_id = %s "
                "AND scheduler_lease_expires_at > CURRENT_TIMESTAMP "
                "ON CONFLICT (task_id, cma_event_id) DO NOTHING",
                (request_id, task_id, cma_event_id, context_id, claim_id),
            )

    def add_clarification_request(
        self, request_id: str, task_id: str, cma_event_id: str, context_id: str, claim_id: str
    ) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cma_clarification_requests "
                "(request_id, task_id, cma_event_id, status) "
                "SELECT %s, %s, %s, 'pending' FROM cma_contexts "
                "WHERE context_id = %s AND scheduler_claim_id = %s "
                "AND scheduler_lease_expires_at > CURRENT_TIMESTAMP "
                "ON CONFLICT (task_id, cma_event_id) DO NOTHING",
                (request_id, task_id, cma_event_id, context_id, claim_id),
            )

    def list_input_requests(self, task_id: str) -> list[dict[str, Any]]:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cma_input_requests WHERE task_id = %s ORDER BY created_at, request_id",
                (task_id,),
            )
            rows = list(cur.fetchall())
            cur.execute(
                "SELECT * FROM cma_clarification_requests WHERE task_id = %s ORDER BY created_at, request_id",
                (task_id,),
            )
            rows.extend(cur.fetchall())
            return sorted(rows, key=lambda row: (row["created_at"], row["request_id"]))

    def get_input_request(self, request_id: str, task_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cma_input_requests WHERE request_id = %s AND task_id = %s",
                (request_id, task_id),
            )
            row = cur.fetchone()
            if row is not None:
                return row
            cur.execute(
                "SELECT * FROM cma_clarification_requests WHERE request_id = %s AND task_id = %s",
                (request_id, task_id),
            )
            return cur.fetchone()

    def decide_clarification_request(
        self, request_id: str, task_id: str, answer_object_key: str
    ) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE cma_clarification_requests SET status = 'resolving', answer_object_key = %s, "
                "updated_at = CURRENT_TIMESTAMP WHERE request_id = %s AND task_id = %s "
                "AND status = 'pending' AND EXISTS "
                "(SELECT 1 FROM cma_tasks t WHERE t.task_id = cma_clarification_requests.task_id "
                "AND (t.internal_state = 'input_required' OR t.cancel_requested_at IS NOT NULL)) "
                "RETURNING *",
                (answer_object_key, request_id, task_id),
            )
            if row := cur.fetchone():
                return row
            cur.execute(
                "SELECT * FROM cma_clarification_requests WHERE request_id = %s AND task_id = %s",
                (request_id, task_id),
            )
            return cur.fetchone()

    def mark_clarification_attempted(self, request_id: str, context_id: str, claim_id: str) -> bool:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE cma_clarification_requests SET response_attempted_at = CURRENT_TIMESTAMP "
                "WHERE request_id = %s AND status = 'resolving' "
                "AND response_attempted_at IS NULL AND EXISTS "
                "(SELECT 1 FROM cma_tasks t JOIN cma_contexts c ON c.context_id = t.context_id "
                "WHERE t.task_id = cma_clarification_requests.task_id AND c.context_id = %s "
                "AND c.scheduler_claim_id = %s AND c.scheduler_lease_expires_at > CURRENT_TIMESTAMP) "
                "RETURNING request_id",
                (request_id, context_id, claim_id),
            )
            return cur.fetchone() is not None

    def decide_input_request(
        self, request_id: str, task_id: str, decision: str, reason_object_key: str | None = None
    ) -> dict[str, Any] | None:
        if decision not in {"allow", "deny"}:
            raise ValueError("Unknown tool decision")
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE cma_input_requests SET status = 'resolving', decision = %s, "
                "decision_reason_object_key = %s, "
                "updated_at = CURRENT_TIMESTAMP WHERE request_id = %s AND task_id = %s "
                "AND status = 'pending' AND EXISTS "
                "(SELECT 1 FROM cma_tasks t WHERE t.task_id = cma_input_requests.task_id "
                "AND (t.internal_state = 'input_required' OR t.cancel_requested_at IS NOT NULL)) "
                "RETURNING *",
                (decision, reason_object_key, request_id, task_id),
            )
            if row := cur.fetchone():
                return row
            cur.execute(
                "SELECT * FROM cma_input_requests WHERE request_id = %s AND task_id = %s",
                (request_id, task_id),
            )
            return cur.fetchone()

    def resolve_input_request(self, request_id: str, context_id: str, claim_id: str) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE cma_input_requests SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE request_id = %s AND status = 'resolving' "
                "AND EXISTS (SELECT 1 FROM cma_tasks t JOIN cma_contexts c "
                "ON c.context_id = t.context_id WHERE t.task_id = cma_input_requests.task_id "
                "AND c.context_id = %s AND c.scheduler_claim_id = %s "
                "AND c.scheduler_lease_expires_at > CURRENT_TIMESTAMP)",
                (request_id, context_id, claim_id),
            )
            cur.execute(
                "UPDATE cma_clarification_requests SET status = 'resolved', "
                "resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE request_id = %s AND status = 'resolving' AND EXISTS "
                "(SELECT 1 FROM cma_tasks t JOIN cma_contexts c ON c.context_id = t.context_id "
                "WHERE t.task_id = cma_clarification_requests.task_id AND c.context_id = %s "
                "AND c.scheduler_claim_id = %s AND c.scheduler_lease_expires_at > CURRENT_TIMESTAMP)",
                (request_id, context_id, claim_id),
            )

    def mark_confirmation_attempted(self, request_id: str, context_id: str, claim_id: str) -> bool:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE cma_input_requests SET confirmation_attempted_at = CURRENT_TIMESTAMP "
                "WHERE request_id = %s AND status = 'resolving' "
                "AND confirmation_attempted_at IS NULL AND EXISTS "
                "(SELECT 1 FROM cma_tasks t JOIN cma_contexts c ON c.context_id = t.context_id "
                "WHERE t.task_id = cma_input_requests.task_id AND c.context_id = %s "
                "AND c.scheduler_claim_id = %s AND c.scheduler_lease_expires_at > CURRENT_TIMESTAMP) "
                "RETURNING request_id",
                (request_id, context_id, claim_id),
            )
            return cur.fetchone() is not None
