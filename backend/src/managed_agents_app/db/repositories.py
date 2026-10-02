from __future__ import annotations

import uuid

from managed_agents_app.config import AppConfig
from managed_agents_app.db.connection import connect


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
