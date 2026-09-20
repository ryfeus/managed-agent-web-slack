from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from managed_agents_app.config import load_config
from managed_agents_app.db.connection import connect
from managed_agents_app.managed_agent import ManagedAgentClient

pytestmark = pytest.mark.integration


def live_config():
    if os.getenv("RUN_INTEGRATION_TESTS") != "1":
        pytest.skip("Set RUN_INTEGRATION_TESTS=1 to call live AWS and Anthropic services")
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    load_config.cache_clear()
    return load_config()


def test_live_dsql_identity() -> None:
    config = live_config()
    # The local SSO principal has DbConnectAdmin. Deployed handlers use their
    # IAM-mapped app_runtime role and are covered by deployed smoke tests.
    with connect(config, "admin") as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1 AS healthy")
        assert cursor.fetchone() == {"healthy": 1}


def test_live_managed_agent_listing() -> None:
    sessions = ManagedAgentClient(live_config()).list_sessions()
    assert isinstance(sessions, list)
