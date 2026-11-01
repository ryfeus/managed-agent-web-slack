"""Metadata-only, fenced Slack ingress and A2A task projection persistence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from managed_agents_app.config import AppConfig
from managed_agents_app.db.connection import connect, with_dsql_retry


@dataclass(frozen=True)
class Claim:
    status: Literal["acquired", "busy", "completed"]
    token: str | None = None
    row: dict[str, Any] | None = None


class SlackA2ARepository:
    def __init__(self, config: AppConfig, role: str | None = None) -> None:
        self.config = config
        self.role = role

    def claim_ingress(self, surface: str, event_id: str, lease_seconds: int = 60) -> Claim:
        def operation() -> Claim:
            token = str(uuid.uuid4())
            with connect(self.config, self.role) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO surface_ingress_events "
                    "(ingress_id, surface, external_event_id, status) VALUES (%s, %s, %s, 'received') "
                    "ON CONFLICT (surface, external_event_id) DO NOTHING",
                    (str(uuid.uuid4()), surface, event_id),
                )
                cur.execute(
                    "UPDATE surface_ingress_events SET status = 'sending', claim_id = %s, "
                    "lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'), "
                    "attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP "
                    "WHERE surface = %s AND external_event_id = %s AND "
                    "(status IN ('received', 'failed') OR "
                    "(status = 'sending' AND lease_expires_at <= CURRENT_TIMESTAMP)) RETURNING *",
                    (token, lease_seconds, surface, event_id),
                )
                if row := cur.fetchone():
                    return Claim("acquired", token, row)
                cur.execute(
                    "SELECT * FROM surface_ingress_events WHERE surface = %s AND external_event_id = %s",
                    (surface, event_id),
                )
                row = cur.fetchone()
                return Claim("completed" if row and row["status"] == "sent" else "busy", row=row)

        return with_dsql_retry(operation)

    def complete_ingress(
        self, surface: str, event_id: str, token: str, thread_id: str | None, task_id: str | None
    ) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE surface_ingress_events SET status = 'sent', thread_id = %s, task_id = %s, "
                "claim_id = NULL, lease_expires_at = NULL, last_error = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE surface = %s AND external_event_id = %s AND claim_id = %s RETURNING ingress_id",
                (thread_id, task_id, surface, event_id, token),
            )
            if cur.fetchone() is None:
                raise RuntimeError("Ingress claim is stale")

    def fail_ingress(self, surface: str, event_id: str, token: str, error: str) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE surface_ingress_events SET status = 'failed', claim_id = NULL, "
                "lease_expires_at = NULL, last_error = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE surface = %s AND external_event_id = %s AND claim_id = %s",
                (error[:1000], surface, event_id, token),
            )

    def ensure_projection(
        self,
        binding_id: str,
        task_id: str,
        request_event_id: str | None = None,
        source_message_ts: str | None = None,
        initiator_user_id: str | None = None,
    ) -> dict[str, Any]:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO slack_task_projections "
                "(binding_id, task_id, request_event_id, source_message_ts, initiator_user_id, status) "
                "VALUES (%s, %s, %s, %s, %s, 'pending') "
                "ON CONFLICT (binding_id, task_id) DO UPDATE SET "
                "request_event_id = COALESCE(slack_task_projections.request_event_id, "
                "EXCLUDED.request_event_id), "
                "source_message_ts = COALESCE(slack_task_projections.source_message_ts, "
                "EXCLUDED.source_message_ts), "
                "initiator_user_id = COALESCE(slack_task_projections.initiator_user_id, "
                "EXCLUDED.initiator_user_id) "
                "RETURNING *",
                (binding_id, task_id, request_event_id, source_message_ts, initiator_user_id),
            )
            row = cur.fetchone()
            assert row is not None
            return row

    def get_projection(self, binding_id: str, task_id: str) -> dict[str, Any] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM slack_task_projections WHERE binding_id = %s AND task_id = %s",
                (binding_id, task_id),
            )
            return cur.fetchone()

    def claim_task_stream(self, binding_id: str, task_id: str, lease_seconds: int = 90) -> Claim:
        token = str(uuid.uuid4())
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE slack_task_projections SET status = 'streaming', claim_id = %s, "
                "lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'), "
                "attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP "
                "WHERE binding_id = %s AND task_id = %s AND "
                "(status IN ('pending', 'fallback', 'completed') OR "
                "(status = 'streaming' AND lease_expires_at <= CURRENT_TIMESTAMP)) "
                "RETURNING *",
                (token, lease_seconds, binding_id, task_id),
            )
            if row := cur.fetchone():
                return Claim("acquired", token, row)
            cur.execute(
                "SELECT * FROM slack_task_projections WHERE binding_id = %s AND task_id = %s",
                (binding_id, task_id),
            )
            row = cur.fetchone()
            return Claim("busy" if row and row["status"] == "streaming" else "completed", row=row)

    def update_stream_message(self, binding_id: str, task_id: str, token: str, message_ts: str) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE slack_task_projections SET slack_message_ts = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE binding_id = %s AND task_id = %s AND claim_id = %s",
                (message_ts, binding_id, task_id, token),
            )

    def finish_stream(
        self,
        binding_id: str,
        task_id: str,
        token: str,
        *,
        fallback: bool,
        state: str | None,
        error: str | None = None,
    ) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE slack_task_projections SET status = %s, last_task_state = %s, "
                "slack_message_ts = CASE WHEN %s THEN NULL ELSE slack_message_ts END, "
                "last_error = %s, claim_id = NULL, lease_expires_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE binding_id = %s AND task_id = %s AND claim_id = %s",
                (
                    "fallback"
                    if fallback
                    else (
                        "completed"
                        if state in {"INPUT_REQUIRED", "COMPLETED", "CANCELED", "FAILED", "REJECTED"}
                        else "pending"
                    ),
                    state,
                    not fallback
                    and state in {"INPUT_REQUIRED", "COMPLETED", "CANCELED", "FAILED", "REJECTED"},
                    error[:1000] if error else None,
                    binding_id,
                    task_id,
                    token,
                ),
            )

    def active_stream(self, binding_id: str, task_id: str) -> bool:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM slack_task_projections WHERE binding_id = %s AND task_id = %s "
                "AND status = 'streaming' AND lease_expires_at > CURRENT_TIMESTAMP",
                (binding_id, task_id),
            )
            return cur.fetchone() is not None

    def mark_projection(self, binding_id: str, task_id: str, state: str, status: str) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE slack_task_projections SET last_task_state = %s, status = %s, "
                "slack_message_ts = CASE WHEN %s THEN NULL ELSE slack_message_ts END, "
                "updated_at = CURRENT_TIMESTAMP WHERE binding_id = %s AND task_id = %s "
                "AND (status != 'streaming' OR lease_expires_at <= CURRENT_TIMESTAMP)",
                (state, status, status == "completed", binding_id, task_id),
            )

    def human_input_messages(self, binding_id: str, task_id: str) -> list[dict[str, Any]]:
        with connect(self.config, self.role) as conn:
            return conn.execute(
                "SELECT item_id, slack_message_ts FROM slack_projection_items "
                "WHERE binding_id = %s AND task_id = %s AND item_kind = 'human_input' "
                "AND status = 'posted' AND slack_message_ts IS NOT NULL",
                (binding_id, task_id),
            ).fetchall()

    def has_human_input_receipt(self, binding_id: str, task_id: str) -> bool:
        # Closed controls retain their receipt even after the Task resumes WORKING.
        with connect(self.config, self.role) as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM slack_projection_items WHERE binding_id = %s AND task_id = %s "
                    "AND item_kind = 'human_input' AND status = 'posted' LIMIT 1",
                    (binding_id, task_id),
                ).fetchone()
                is not None
            )

    def close_human_input(self, binding_id: str, task_id: str, item_id: str) -> None:
        with connect(self.config, self.role) as conn:
            conn.execute(
                "UPDATE slack_projection_items SET slack_message_ts = NULL "
                "WHERE binding_id = %s AND task_id = %s AND item_kind = 'human_input' AND item_id = %s",
                (binding_id, task_id, item_id),
            )

    def claim_item(
        self, binding_id: str, task_id: str, kind: str, item_id: str, lease_seconds: int = 60
    ) -> Claim:
        def operation() -> Claim:
            token = str(uuid.uuid4())
            with connect(self.config, self.role) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO slack_projection_items "
                    "(binding_id, task_id, item_kind, item_id, status) VALUES (%s, %s, %s, %s, 'failed') "
                    "ON CONFLICT (binding_id, task_id, item_kind, item_id) DO NOTHING",
                    (binding_id, task_id, kind, item_id),
                )
                cur.execute(
                    "UPDATE slack_projection_items SET status = 'posting', claim_id = %s, "
                    "lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'), "
                    "attempts = attempts + 1 WHERE binding_id = %s AND task_id = %s "
                    "AND item_kind = %s AND item_id = %s AND "
                    "(status = 'failed' OR (status = 'posting' AND lease_expires_at <= CURRENT_TIMESTAMP)) "
                    "RETURNING *",
                    (token, lease_seconds, binding_id, task_id, kind, item_id),
                )
                if row := cur.fetchone():
                    return Claim("acquired", token, row)
                cur.execute(
                    "SELECT * FROM slack_projection_items WHERE binding_id = %s AND task_id = %s "
                    "AND item_kind = %s AND item_id = %s",
                    (binding_id, task_id, kind, item_id),
                )
                row = cur.fetchone()
                return Claim("completed" if row and row["status"] == "posted" else "busy", row=row)

        return with_dsql_retry(operation)

    def finish_item(
        self,
        binding_id: str,
        task_id: str,
        kind: str,
        item_id: str,
        token: str,
        message_ts: str | None,
        error: str | None = None,
    ) -> None:
        projected_at = "NULL" if error else "CURRENT_TIMESTAMP"
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE slack_projection_items SET status = %s, slack_message_ts = %s, "
                f"last_error = %s, projected_at = {projected_at}, "
                "claim_id = NULL, lease_expires_at = NULL WHERE binding_id = %s AND task_id = %s "
                "AND item_kind = %s AND item_id = %s AND claim_id = %s",
                (
                    "failed" if error else "posted",
                    message_ts,
                    error[:1000] if error else None,
                    binding_id,
                    task_id,
                    kind,
                    item_id,
                    token,
                ),
            )
