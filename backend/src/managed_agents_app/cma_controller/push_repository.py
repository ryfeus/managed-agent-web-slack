"""Metadata-only controller push configuration and delivery leases."""

from __future__ import annotations

import uuid
from typing import Any

from managed_agents_app.cma_controller.runtime_repository import ControllerRuntimeRepository
from managed_agents_app.db.connection import connect, with_dsql_retry


class PushConfigConflict(ValueError):
    pass


class PushRepository:
    def __init__(self, runtime: ControllerRuntimeRepository) -> None:
        self.runtime = runtime

    def create(self, task_id: str, config_id: str, url: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            with connect(self.runtime.config, self.runtime.role) as conn, conn.cursor() as cur:
                cur.execute("SELECT task_id FROM cma_tasks WHERE task_id = %s", (task_id,))
                if cur.fetchone() is None:
                    raise KeyError(task_id)
                cur.execute(
                    "INSERT INTO cma_push_configs (task_id, config_id, url) VALUES (%s, %s, %s) "
                    "ON CONFLICT (task_id, config_id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP "
                    "RETURNING *",
                    (task_id, config_id, url),
                )
                row = cur.fetchone()
                assert row is not None
                if row["url"] != url or row["deleted_at"] is not None:
                    raise PushConfigConflict("Push configuration ID is already used")
                return row

        return with_dsql_retry(operation)

    def get(self, task_id: str, config_id: str) -> dict[str, Any] | None:
        with connect(self.runtime.config, self.runtime.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cma_push_configs WHERE task_id = %s AND config_id = %s AND deleted_at IS NULL",
                (task_id, config_id),
            )
            return cur.fetchone()

    def list_permanently_failed(self) -> list[dict[str, Any]]:
        with connect(self.runtime.config, self.runtime.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT p.*, t.context_id FROM cma_push_configs p "
                "JOIN cma_tasks t ON t.task_id = p.task_id "
                "WHERE p.deleted_at IS NULL AND p.permanent_failure_at IS NOT NULL "
                "ORDER BY p.task_id, p.config_id"
            )
            return list(cur.fetchall())

    def reset_permanent_failure(self, task_id: str, config_id: str) -> bool:
        with connect(self.runtime.config, self.runtime.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE cma_push_configs SET permanent_failure_at = NULL, attempt_count = 0, "
                "next_attempt_at = NULL, delivery_claim_id = NULL, delivery_lease_expires_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE task_id = %s AND config_id = %s AND deleted_at IS NULL "
                "AND permanent_failure_at IS NOT NULL "
                "AND (delivery_claim_id IS NULL OR delivery_lease_expires_at <= CURRENT_TIMESTAMP) "
                "RETURNING task_id",
                (task_id, config_id),
            )
            return cur.fetchone() is not None

    def list_configs(self, task_id: str, after: str = "", limit: int = 50) -> list[dict[str, Any]]:
        with connect(self.runtime.config, self.runtime.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM cma_push_configs WHERE task_id = %s AND config_id > %s "
                "AND deleted_at IS NULL ORDER BY config_id LIMIT %s",
                (task_id, after, limit),
            )
            return list(cur.fetchall())

    def delete(self, task_id: str, config_id: str) -> bool:
        with connect(self.runtime.config, self.runtime.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE cma_push_configs SET deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                "WHERE task_id = %s AND config_id = %s AND deleted_at IS NULL RETURNING task_id",
                (task_id, config_id),
            )
            return cur.fetchone() is not None

    def deletion_settled(self, task_id: str, config_id: str) -> bool:
        with connect(self.runtime.config, self.runtime.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT delivery_claim_id, delivery_lease_expires_at <= CURRENT_TIMESTAMP AS expired "
                "FROM cma_push_configs "
                "WHERE task_id = %s AND config_id = %s",
                (task_id, config_id),
            )
            row = cur.fetchone()
            return row is None or row["delivery_claim_id"] is None or bool(row["expired"])

    def purge_deleted(self, task_id: str, config_id: str) -> None:
        with connect(self.runtime.config, self.runtime.role) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM cma_push_configs WHERE task_id = %s AND config_id = %s "
                "AND deleted_at IS NOT NULL AND "
                "(delivery_claim_id IS NULL OR delivery_lease_expires_at <= CURRENT_TIMESTAMP)",
                (task_id, config_id),
            )

    def contexts_for_maintenance(self, after_context_id: str = "", limit: int = 500) -> list[str]:
        with connect(self.runtime.config, self.runtime.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT t.context_id FROM cma_push_configs p "
                "JOIN cma_tasks t ON t.task_id = p.task_id "
                "WHERE p.deleted_at IS NULL AND p.permanent_failure_at IS NULL "
                "AND t.context_id > %s "
                "ORDER BY t.context_id LIMIT %s",
                (after_context_id, limit),
            )
            return [str(row["context_id"]) for row in cur.fetchall()]

    def list_context(self, context_id: str) -> list[dict[str, Any]]:
        with connect(self.runtime.config, self.runtime.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT p.* FROM cma_push_configs p JOIN cma_tasks t ON t.task_id = p.task_id "
                "WHERE t.context_id = %s AND p.deleted_at IS NULL AND p.permanent_failure_at IS NULL "
                "ORDER BY p.task_id, p.config_id",
                (context_id,),
            )
            return list(cur.fetchall())

    def claim(self, task_id: str, config_id: str, lease_seconds: int = 30) -> str | None:
        claim_id = str(uuid.uuid4())
        with connect(self.runtime.config, self.runtime.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE cma_push_configs SET delivery_claim_id = %s, "
                "delivery_lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second') "
                "WHERE task_id = %s AND config_id = %s AND deleted_at IS NULL "
                "AND permanent_failure_at IS NULL AND "
                "(next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP) AND "
                "(delivery_claim_id IS NULL OR delivery_lease_expires_at <= CURRENT_TIMESTAMP) "
                "RETURNING task_id",
                (claim_id, lease_seconds, task_id, config_id),
            )
            return claim_id if cur.fetchone() else None

    def claimed(self, task_id: str, config_id: str, claim_id: str) -> bool:
        with connect(self.runtime.config, self.runtime.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM cma_push_configs WHERE task_id = %s AND config_id = %s "
                "AND delivery_claim_id = %s AND delivery_lease_expires_at > CURRENT_TIMESTAMP "
                "AND deleted_at IS NULL",
                (task_id, config_id, claim_id),
            )
            return cur.fetchone() is not None

    def finish(
        self,
        task_id: str,
        config_id: str,
        claim_id: str,
        *,
        fingerprint: str | None,
        retry_seconds: int | None = None,
        permanent: bool = False,
    ) -> bool:
        if fingerprint is not None:
            update = (
                "last_delivered_fingerprint = %s, last_delivered_at = CURRENT_TIMESTAMP, "
                "attempt_count = 0, next_attempt_at = NULL"
            )
            values: tuple[object, ...] = (fingerprint,)
        elif permanent:
            update = "attempt_count = attempt_count + 1, permanent_failure_at = CURRENT_TIMESTAMP"
            values = ()
        else:
            update = (
                "attempt_count = attempt_count + 1, "
                "next_attempt_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second')"
            )
            values = (retry_seconds or 5,)
        with connect(self.runtime.config, self.runtime.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE cma_push_configs SET delivery_claim_id = NULL, delivery_lease_expires_at = NULL, "
                f"last_attempt_at = CURRENT_TIMESTAMP, {update}, "
                "updated_at = CURRENT_TIMESTAMP WHERE task_id = %s AND config_id = %s "
                "AND delivery_claim_id = %s AND delivery_lease_expires_at > CURRENT_TIMESTAMP "
                "RETURNING task_id",
                (*values, task_id, config_id, claim_id),
            )
            return cur.fetchone() is not None

    def release(self, task_id: str, config_id: str, claim_id: str) -> None:
        with connect(self.runtime.config, self.runtime.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE cma_push_configs SET delivery_claim_id = NULL, delivery_lease_expires_at = NULL "
                "WHERE task_id = %s AND config_id = %s AND delivery_claim_id = %s",
                (task_id, config_id, claim_id),
            )
