from __future__ import annotations

import sys
import uuid
from pathlib import Path

import psycopg
import pytest

from managed_agents_app.config import load_config
from managed_agents_app.testing.factory import validate_local_database

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from mcp_admin import link_identity


def test_mcp_identity_link_is_idempotent_and_cannot_reassign_owner():
    config = load_config()
    validate_local_database(config)
    first, other, subject = (str(uuid.uuid4()) for _ in range(3))
    issuer = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_test"
    with psycopg.connect(config.database_url) as conn:
        conn.execute("INSERT INTO principals (principal_id) VALUES (%s), (%s)", (first, other))
        link_identity(conn, issuer, subject, first)
        link_identity(conn, issuer, subject, first)
        with pytest.raises(ValueError, match="refusing reassignment"):
            link_identity(conn, issuer, subject, other)
        with pytest.raises(ValueError, match="already exist"):
            link_identity(conn, issuer, "unlinked", str(uuid.uuid4()))
        row = conn.execute(
            "SELECT principal_id FROM external_identities WHERE provider='cognito' "
            "AND tenant_id=%s AND external_user_id=%s",
            (issuer, subject),
        ).fetchone()
        assert row and str(row[0]) == first
        conn.rollback()
