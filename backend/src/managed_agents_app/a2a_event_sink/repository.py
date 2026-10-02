"""Metadata-only receipt and per-task observation claims."""

from __future__ import annotations

import uuid
from typing import Any

from managed_agents_app.db.connection import connect, with_dsql_retry
from managed_agents_app.db.thread_repository import ThreadRepository


class ReceiptConflict(ValueError):
    pass


class EventSinkRepository:
    def __init__(self, threads: ThreadRepository) -> None:
        self.threads = threads

    def route(self, agent_id: str, task_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        binding = self.threads.get_task_binding(agent_id, task_id)
        if binding is None:
            return None
        thread = self.threads.get_thread(str(binding["thread_id"]))
        return (thread, binding) if thread is not None else None

    def receipt(
        self, agent_id: str, delivery_id: str, task_id: str, thread_id: str, context_id: str, event_kind: str
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with connect(self.threads.config, self.threads.role) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO a2a_task_event_receipts "
                    "(receipt_id, agent_id, delivery_id, task_id, thread_id, context_id, event_kind) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (agent_id, delivery_id) DO UPDATE SET "
                    "delivery_id = EXCLUDED.delivery_id RETURNING *",
                    (str(uuid.uuid4()), agent_id, delivery_id, task_id, thread_id, context_id, event_kind),
                )
                row = cur.fetchone()
                assert row is not None
                if (
                    str(row["task_id"]) != task_id
                    or str(row["thread_id"]) != thread_id
                    or str(row["context_id"]) != context_id
                    or row["event_kind"] != event_kind
                ):
                    raise ReceiptConflict("Delivery ID was reused for a different Task")
                return row

        return with_dsql_retry(operation)

    def claim_task(self, agent_id: str, task_id: str, lease_seconds: int = 30) -> str | None:
        claim = str(uuid.uuid4())

        def operation() -> str | None:
            with connect(self.threads.config, self.threads.role) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO a2a_task_observation_claims (agent_id, task_id, claim_id, lease_expires_at) "
                    "VALUES (%s, %s, %s, CURRENT_TIMESTAMP + (%s * INTERVAL '1 second')) "
                    "ON CONFLICT (agent_id, task_id) DO UPDATE SET claim_id = EXCLUDED.claim_id, "
                    "lease_expires_at = EXCLUDED.lease_expires_at "
                    "WHERE a2a_task_observation_claims.lease_expires_at <= CURRENT_TIMESTAMP "
                    "OR a2a_task_observation_claims.claim_id IS NULL RETURNING claim_id",
                    (agent_id, task_id, claim, lease_seconds),
                )
                return claim if cur.fetchone() else None

        return with_dsql_retry(operation)

    def observe(self, agent_id: str, task_id: str, claim: str, delivery_id: str, state: str) -> None:
        with connect(self.threads.config, self.threads.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_tasks SET last_seen_task_state = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE agent_id = %s AND task_id = %s AND EXISTS "
                "(SELECT 1 FROM a2a_task_observation_claims c WHERE c.agent_id = %s "
                "AND c.task_id = %s AND c.claim_id = %s AND c.lease_expires_at > CURRENT_TIMESTAMP) "
                "RETURNING task_id",
                (state, agent_id, task_id, agent_id, task_id, claim),
            )
            if cur.fetchone() is None:
                raise RuntimeError("Task observation claim expired")
            cur.execute(
                "UPDATE a2a_task_event_receipts SET observed_task_state = %s "
                "WHERE agent_id = %s AND delivery_id = %s",
                (state, agent_id, delivery_id),
            )

    def mark_published(self, agent_id: str, delivery_id: str) -> None:
        with connect(self.threads.config, self.threads.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE a2a_task_event_receipts SET published_at = CURRENT_TIMESTAMP "
                "WHERE agent_id = %s AND delivery_id = %s",
                (agent_id, delivery_id),
            )

    def release_task(self, agent_id: str, task_id: str, claim: str) -> None:
        with connect(self.threads.config, self.threads.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE a2a_task_observation_claims SET claim_id = NULL, lease_expires_at = NULL "
                "WHERE agent_id = %s AND task_id = %s AND claim_id = %s",
                (agent_id, task_id, claim),
            )
