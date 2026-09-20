from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any, Literal

from managed_agents_app.config import AppConfig
from managed_agents_app.db.connection import connect, with_dsql_retry


class IngressClaim(StrEnum):
    ACQUIRED = "acquired"
    COMPLETED = "completed"
    BUSY = "busy"


class ProjectionClaim(StrEnum):
    ACQUIRED = "acquired"
    COMPLETED = "completed"
    BUSY = "busy"


class Database:
    def __init__(self, config: AppConfig, role: str | None = None) -> None:
        self.config = config
        self.role = role

    def ensure_principal(self, principal_id: str, display_name: str = "Developer") -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO principals (principal_id, display_name) VALUES (%s, %s) "
                "ON CONFLICT (principal_id) DO NOTHING",
                (principal_id, display_name),
            )

    def map_external_identity(
        self, principal_id: str, provider: str, tenant_id: str, external_user_id: str
    ) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO external_identities "
                "(identity_id, principal_id, provider, tenant_id, external_user_id) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (provider, tenant_id, external_user_id) "
                "DO UPDATE SET principal_id = EXCLUDED.principal_id",
                (str(uuid.uuid4()), principal_id, provider, tenant_id, external_user_id),
            )

    def resolve_external_identity(self, provider: str, tenant_id: str, external_user_id: str) -> str | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT principal_id FROM external_identities "
                "WHERE provider = %s AND tenant_id = %s AND external_user_id = %s LIMIT 1",
                (provider, tenant_id, external_user_id),
            )
            row = cur.fetchone()
            return str(row["principal_id"]) if row else None

    def register_session(
        self,
        session_id: str,
        principal_id: str,
        agent_id: str,
        environment_id: str,
        surface: Literal["web", "slack"],
    ) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            self._register_session(cur, session_id, principal_id, agent_id, environment_id, surface)

    @staticmethod
    def _register_session(
        cur: Any,
        session_id: str,
        principal_id: str,
        agent_id: str,
        environment_id: str,
        surface: str,
    ) -> None:
        cur.execute(
            "INSERT INTO agent_sessions "
            "(session_id, principal_id, agent_id, environment_id, created_by_surface) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (session_id) DO NOTHING",
            (session_id, principal_id, agent_id, environment_id, surface),
        )

    def list_sessions(self, principal_id: str) -> list[dict[str, Any]]:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT session_id, principal_id, agent_id, environment_id, created_by_surface, "
                "created_at, archived_at FROM agent_sessions "
                "WHERE principal_id = %s AND archived_at IS NULL ORDER BY created_at DESC",
                (principal_id,),
            )
            return list(cur.fetchall())

    def owns_session(self, principal_id: str, session_id: str) -> bool:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM agent_sessions WHERE principal_id = %s AND session_id = %s "
                "AND archived_at IS NULL LIMIT 1",
                (principal_id, session_id),
            )
            return cur.fetchone() is not None

    def mark_session_archived(self, principal_id: str, session_id: str) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_sessions SET archived_at = CURRENT_TIMESTAMP "
                "WHERE principal_id = %s AND session_id = %s",
                (principal_id, session_id),
            )

    def get_slack_binding(self, team_id: str, channel_id: str, thread_ts: str) -> dict[str, Any] | None:
        external_thread_id = f"{channel_id}:{thread_ts}"
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT binding_id, session_id, surface, tenant_id, external_thread_id "
                "FROM surface_bindings WHERE surface = 'slack' AND tenant_id = %s "
                "AND external_thread_id = %s LIMIT 1",
                (team_id, external_thread_id),
            )
            return cur.fetchone()

    def bind_slack_thread(
        self, session_id: str, team_id: str, channel_id: str, thread_ts: str
    ) -> dict[str, Any]:
        return with_dsql_retry(lambda: self._bind_slack_thread(session_id, team_id, channel_id, thread_ts))

    def _bind_slack_thread(
        self, session_id: str, team_id: str, channel_id: str, thread_ts: str
    ) -> dict[str, Any]:
        external_thread_id = f"{channel_id}:{thread_ts}"
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO surface_bindings "
                "(binding_id, session_id, surface, tenant_id, external_thread_id) "
                "VALUES (%s, %s, 'slack', %s, %s) "
                "ON CONFLICT (surface, tenant_id, external_thread_id) "
                "DO UPDATE SET external_thread_id = EXCLUDED.external_thread_id "
                "RETURNING binding_id, session_id, surface, tenant_id, external_thread_id",
                (str(uuid.uuid4()), session_id, team_id, external_thread_id),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("Failed to bind Slack thread")
            return row

    def register_and_bind(
        self,
        session_id: str,
        principal_id: str,
        agent_id: str,
        environment_id: str,
        team_id: str,
        channel_id: str,
        thread_ts: str,
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            external_thread_id = f"{channel_id}:{thread_ts}"
            with connect(self.config, self.role) as conn, conn.cursor() as cur:
                self._register_session(cur, session_id, principal_id, agent_id, environment_id, "slack")
                cur.execute(
                    "INSERT INTO surface_bindings "
                    "(binding_id, session_id, surface, tenant_id, external_thread_id) "
                    "VALUES (%s, %s, 'slack', %s, %s) "
                    "ON CONFLICT (surface, tenant_id, external_thread_id) "
                    "DO UPDATE SET external_thread_id = EXCLUDED.external_thread_id "
                    "RETURNING binding_id, session_id, surface, tenant_id, external_thread_id",
                    (str(uuid.uuid4()), session_id, team_id, external_thread_id),
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("Failed to create Slack binding")
                return row

        return with_dsql_retry(operation)

    def list_slack_bindings(self, session_id: str) -> list[dict[str, Any]]:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT binding_id, session_id, surface, tenant_id, external_thread_id "
                "FROM surface_bindings WHERE surface = 'slack' AND session_id = %s",
                (session_id,),
            )
            return list(cur.fetchall())

    def claim_ingress(self, surface: str, external_event_id: str) -> IngressClaim:
        def operation() -> IngressClaim:
            with connect(self.config, self.role) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ingress_events (ingress_id, surface, external_event_id, status) "
                    "VALUES (%s, %s, %s, 'received') ON CONFLICT (surface, external_event_id) DO NOTHING",
                    (str(uuid.uuid4()), surface, external_event_id),
                )
                cur.execute(
                    "SELECT status, lease_expires_at FROM ingress_events "
                    "WHERE surface = %s AND external_event_id = %s",
                    (surface, external_event_id),
                )
                row = cur.fetchone()
                if row and row["status"] == "sent":
                    return IngressClaim.COMPLETED
                cur.execute(
                    "UPDATE ingress_events SET status = 'sending', "
                    "lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '2 minutes', attempts = attempts + 1, "
                    "updated_at = CURRENT_TIMESTAMP WHERE surface = %s AND external_event_id = %s "
                    "AND NOT (status = 'sending' AND lease_expires_at > CURRENT_TIMESTAMP) "
                    "RETURNING ingress_id",
                    (surface, external_event_id),
                )
                return IngressClaim.ACQUIRED if cur.fetchone() else IngressClaim.BUSY

        return with_dsql_retry(operation)

    def complete_ingress(
        self,
        surface: str,
        external_event_id: str,
        session_id: str | None,
        managed_event_id: str | None = None,
    ) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE ingress_events SET status = 'sent', session_id = %s, managed_event_id = %s, "
                "lease_expires_at = NULL, last_error = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE surface = %s AND external_event_id = %s",
                (session_id, managed_event_id, surface, external_event_id),
            )

    def fail_ingress(self, surface: str, external_event_id: str, message: str) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE ingress_events SET status = 'failed', lease_expires_at = NULL, last_error = %s, "
                "updated_at = CURRENT_TIMESTAMP WHERE surface = %s AND external_event_id = %s",
                (message[:1000], surface, external_event_id),
            )

    def claim_projection(self, binding_id: str, managed_event_id: str) -> ProjectionClaim:
        def operation() -> ProjectionClaim:
            with connect(self.config, self.role) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO projection_events (binding_id, managed_event_id, status) "
                    "VALUES (%s, %s, 'failed') ON CONFLICT (binding_id, managed_event_id) DO NOTHING",
                    (binding_id, managed_event_id),
                )
                cur.execute(
                    "SELECT status FROM projection_events WHERE binding_id = %s AND managed_event_id = %s",
                    (binding_id, managed_event_id),
                )
                row = cur.fetchone()
                if row and row["status"] == "posted":
                    return ProjectionClaim.COMPLETED
                cur.execute(
                    "UPDATE projection_events SET status = 'posting', "
                    "lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '2 minutes', attempts = attempts + 1, "
                    "last_error = NULL WHERE binding_id = %s AND managed_event_id = %s "
                    "AND NOT (status = 'posting' AND lease_expires_at > CURRENT_TIMESTAMP) "
                    "RETURNING binding_id",
                    (binding_id, managed_event_id),
                )
                return ProjectionClaim.ACQUIRED if cur.fetchone() else ProjectionClaim.BUSY

        return with_dsql_retry(operation)

    def mark_projected(self, binding_id: str, managed_event_id: str, slack_message_ts: str) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projection_events "
                "(binding_id, managed_event_id, status, slack_message_ts, projected_at) "
                "VALUES (%s, %s, 'posted', %s, CURRENT_TIMESTAMP) "
                "ON CONFLICT (binding_id, managed_event_id) DO UPDATE SET status = 'posted', "
                "slack_message_ts = EXCLUDED.slack_message_ts, projected_at = EXCLUDED.projected_at, "
                "lease_expires_at = NULL, last_error = NULL",
                (binding_id, managed_event_id, slack_message_ts),
            )

    def fail_projection(self, binding_id: str, managed_event_id: str, message: str) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE projection_events SET status = 'failed', lease_expires_at = NULL, last_error = %s "
                "WHERE binding_id = %s AND managed_event_id = %s",
                (message[:1000], binding_id, managed_event_id),
            )

    def store_feedback(
        self,
        interaction_id: str,
        principal_id: str,
        session_id: str,
        managed_event_id: str | None,
        external_message_id: str | None,
        rating: str,
        reason: str | None,
    ) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_feedback (feedback_id, interaction_id, principal_id, session_id, "
                "managed_event_id, surface, external_message_id, rating, reason) "
                "VALUES (%s, %s, %s, %s, %s, 'slack', %s, %s, %s) "
                "ON CONFLICT (interaction_id) DO NOTHING",
                (
                    str(uuid.uuid4()),
                    interaction_id,
                    principal_id,
                    session_id,
                    managed_event_id,
                    external_message_id,
                    rating,
                    reason,
                ),
            )

    def claim_stream(self, binding_id: str, request_id: str, session_id: str) -> ProjectionClaim:
        def operation() -> ProjectionClaim:
            with connect(self.config, self.role) as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO slack_response_streams "
                    "(binding_id, request_event_id, session_id, status) VALUES (%s, %s, %s, 'pending') "
                    "ON CONFLICT (binding_id, request_event_id) DO NOTHING",
                    (binding_id, request_id, session_id),
                )
                cur.execute(
                    "SELECT status FROM slack_response_streams "
                    "WHERE binding_id = %s AND request_event_id = %s",
                    (binding_id, request_id),
                )
                row = cur.fetchone()
                if row and row["status"] in {"completed", "fallback"}:
                    return ProjectionClaim.COMPLETED
                cur.execute(
                    "UPDATE slack_response_streams SET status = 'streaming', "
                    "lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '15 minutes', attempts = attempts + 1, "
                    "last_error = NULL, updated_at = CURRENT_TIMESTAMP "
                    "WHERE binding_id = %s AND request_event_id = %s "
                    "AND NOT (status = 'streaming' AND lease_expires_at > CURRENT_TIMESTAMP) "
                    "RETURNING binding_id",
                    (binding_id, request_id),
                )
                return ProjectionClaim.ACQUIRED if cur.fetchone() else ProjectionClaim.BUSY

        return with_dsql_retry(operation)

    def update_stream(
        self,
        binding_id: str,
        request_id: str,
        *,
        slack_message_ts: str | None = None,
        last_managed_event_id: str | None = None,
    ) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE slack_response_streams SET slack_message_ts = COALESCE(%s, slack_message_ts), "
                "last_managed_event_id = COALESCE(%s, last_managed_event_id), updated_at = CURRENT_TIMESTAMP "
                "WHERE binding_id = %s AND request_event_id = %s",
                (slack_message_ts, last_managed_event_id, binding_id, request_id),
            )

    def complete_stream(
        self,
        binding_id: str,
        request_id: str,
        final_managed_event_id: str | None,
        slack_message_ts: str | None,
        *,
        fallback: bool = False,
        error: str | None = None,
    ) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE slack_response_streams SET status = %s, final_managed_event_id = %s, "
                "slack_message_ts = COALESCE(%s, slack_message_ts), lease_expires_at = NULL, "
                "last_error = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE binding_id = %s AND request_event_id = %s",
                (
                    "fallback" if fallback else "completed",
                    final_managed_event_id,
                    slack_message_ts,
                    error[:1000] if error else None,
                    binding_id,
                    request_id,
                ),
            )

    def recoverable_stream_message(self, binding_id: str, session_id: str) -> str | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT slack_message_ts FROM slack_response_streams "
                "WHERE binding_id = %s AND session_id = %s AND status IN ('streaming', 'fallback') "
                "AND slack_message_ts IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
                (binding_id, session_id),
            )
            row = cur.fetchone()
            return str(row["slack_message_ts"]) if row else None

    def has_active_stream(self, binding_id: str, session_id: str) -> bool:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM slack_response_streams WHERE binding_id = %s AND session_id = %s "
                "AND status = 'streaming' AND lease_expires_at > CURRENT_TIMESTAMP LIMIT 1",
                (binding_id, session_id),
            )
            return cur.fetchone() is not None

    def find_recoverable_stream(self, binding_id: str, session_id: str) -> tuple[str, str] | None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT request_event_id, slack_message_ts FROM slack_response_streams "
                "WHERE binding_id = %s AND session_id = %s "
                "AND (status = 'fallback' OR (status = 'streaming' "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP))) "
                "AND slack_message_ts IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
                (binding_id, session_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            return str(row["request_event_id"]), str(row["slack_message_ts"])

    def complete_recovered_stream(
        self,
        binding_id: str,
        request_id: str,
        managed_event_id: str,
        slack_message_ts: str,
    ) -> None:
        with connect(self.config, self.role) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE slack_response_streams SET status = 'completed', final_managed_event_id = %s, "
                "slack_message_ts = %s, lease_expires_at = NULL, last_error = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE binding_id = %s AND request_event_id = %s",
                (managed_event_id, slack_message_ts, binding_id, request_id),
            )
            cur.execute(
                "INSERT INTO projection_events "
                "(binding_id, managed_event_id, status, slack_message_ts, projected_at) "
                "VALUES (%s, %s, 'posted', %s, CURRENT_TIMESTAMP) "
                "ON CONFLICT (binding_id, managed_event_id) DO UPDATE SET status = 'posted', "
                "slack_message_ts = EXCLUDED.slack_message_ts, projected_at = EXCLUDED.projected_at, "
                "lease_expires_at = NULL, last_error = NULL",
                (binding_id, managed_event_id, slack_message_ts),
            )
