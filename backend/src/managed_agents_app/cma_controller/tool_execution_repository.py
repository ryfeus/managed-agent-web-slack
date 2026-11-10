"""Fenced metadata transitions for application-owned tool executions."""

from __future__ import annotations

import uuid
from typing import Any

from managed_agents_app.cma_controller.runtime_repository import (
    ControllerRuntimeRepository,
    LostSchedulerClaim,
)
from managed_agents_app.db.connection import connect, with_dsql_retry
from managed_agents_app.policy.models import PolicyCheckpoint


class ToolExecutionRepository:
    def __init__(self, repository: ControllerRuntimeRepository) -> None:
        self.repository = repository

    def get(self, task_id: str, event_id: str) -> dict[str, Any] | None:
        with connect(self.repository.config, self.repository.role) as conn:
            return conn.execute(
                "SELECT * FROM cma_tool_executions WHERE task_id = %s AND cma_event_id = %s",
                (task_id, event_id),
            ).fetchone()

    def prepare(
        self, context_id: str, task_id: str, event_id: str, claim: str, digest: str, code: str | None
    ) -> dict[str, Any]:
        invocation = str(uuid.uuid5(uuid.NAMESPACE_URL, f"custom-tool:{task_id}:{event_id}"))
        with connect(self.repository.config, self.repository.role) as conn:
            conn.execute(
                "INSERT INTO cma_tool_executions "
                "(invocation_id, task_id, cma_event_id, call_digest, state, result_object_key, control_code) "
                "SELECT %s, %s, %s, %s, 'pending', %s, %s FROM cma_contexts c "
                "JOIN cma_tasks t ON t.task_id = c.active_task_id "
                "WHERE c.context_id = %s AND t.task_id = %s AND t.internal_state <> 'terminal' "
                "AND c.scheduler_claim_id = %s AND c.scheduler_lease_expires_at > CURRENT_TIMESTAMP "
                "ON CONFLICT (task_id, cma_event_id) DO NOTHING",
                (invocation, task_id, event_id, digest, str(uuid.uuid4()), code, context_id, task_id, claim),
            )
        row = self.get(task_id, event_id)
        if row and row["call_digest"] != digest:
            # Pending review follows canonical changes; the native fingerprint
            # invalidates the old ruling and the existing review path rotates its ID.
            # Once authorization commits, this identity is immutable.
            with connect(self.repository.config, self.repository.role) as conn:
                conn.execute(
                    "UPDATE cma_tool_executions e SET call_digest = %s, control_code = %s, "
                    "updated_at = CURRENT_TIMESTAMP WHERE e.invocation_id = %s AND e.state = 'pending' "
                    "AND e.authorization_object_key IS NULL AND e.execution_attempted_at IS NULL "
                    "AND EXISTS (SELECT 1 FROM cma_input_requests r "
                    "JOIN cma_tasks t ON t.task_id = r.task_id "
                    "JOIN cma_contexts c ON c.context_id = t.context_id WHERE r.task_id = e.task_id "
                    "AND r.cma_event_id = e.cma_event_id AND r.status = 'pending' AND r.decision IS NULL "
                    "AND c.active_task_id = t.task_id AND t.internal_state <> 'terminal' "
                    "AND t.cancel_requested_at IS NULL AND c.context_id = %s AND c.scheduler_claim_id = %s "
                    "AND c.scheduler_lease_expires_at > CURRENT_TIMESTAMP FOR UPDATE OF r, t, c)",
                    (digest, code, invocation, context_id, claim),
                )
            row = self.get(task_id, event_id)
        if row is None or row["call_digest"] != digest:
            raise LostSchedulerClaim(context_id)
        return row

    def transition(
        self,
        row: dict[str, Any],
        context_id: str,
        claim: str,
        state: str,
        *,
        cancelled: bool = False,
        checkpoint: PolicyCheckpoint | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        allowed = {
            "result_digest",
            "control_code",
            "execution_attempted_at",
            "delivery_attempted_at",
            "admitted_result_object_key",
            "admitted_result_digest",
        }
        if not set(fields) <= allowed:
            raise ValueError("Unknown tool execution update")
        fields = {"state": state, **fields}
        assignments = ", ".join(f"{field} = %s" for field in fields)
        cancel_guard = "IS NOT NULL" if cancelled else "IS NULL"

        def operation() -> dict[str, Any]:
            with connect(self.repository.config, self.repository.role) as conn:
                result = conn.execute(
                    f"UPDATE cma_tool_executions SET {assignments}, updated_at = CURRENT_TIMESTAMP "
                    "WHERE invocation_id = %s AND state = %s AND EXISTS "
                    "(SELECT 1 FROM cma_tasks t JOIN cma_contexts c ON c.context_id = t.context_id "
                    "WHERE t.task_id = cma_tool_executions.task_id AND c.context_id = %s "
                    "AND c.active_task_id = t.task_id AND t.internal_state <> 'terminal' "
                    f"AND t.cancel_requested_at {cancel_guard} AND c.scheduler_claim_id = %s "
                    "AND c.scheduler_lease_expires_at > CURRENT_TIMESTAMP FOR UPDATE OF t, c) RETURNING *",
                    (*fields.values(), row["invocation_id"], row["state"], context_id, claim),
                ).fetchone()
                if result is None:
                    raise LostSchedulerClaim(context_id)
                with conn.cursor() as cursor:
                    self.repository.commit_policy_checkpoint(
                        cursor, context_id, str(row["task_id"]), claim, checkpoint
                    )
                return result

        return with_dsql_retry(operation)
