from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

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
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("cma_lock", root / "scripts/cma_lock.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resolved = module.read_lock(root / "cma")
    for variable, field in (
        ("CLAUDE_AGENT_ID", "agent_id"),
        ("CLAUDE_AGENT_VERSION", "agent_version"),
        ("CLAUDE_ENVIRONMENT_ID", "environment_id"),
    ):
        os.environ[variable] = str(resolved[field])
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


def test_live_managed_agent_exact_pin() -> None:
    config = live_config()
    client = ManagedAgentClient(config)
    # Set this to the context produced by the deployed real-Slack-user smoke
    # to prove the running application's selection. Without it, exercise the
    # production controller adapter with a labelled, isolated provider session.
    context_id = os.getenv("CMA_ACCEPTANCE_CONTEXT_ID")
    if context_id:
        created = next(s for s in client.list_sessions() if s.metadata.get("a2a_context_id") == context_id)
    else:
        created = client.create_controller_session(
            context_id=f"cma-config-acceptance-{uuid4()}",
            creation_message_id=str(uuid4()),
            initial_text="Reply exactly CMA_VERSION_PIN_ACCEPTED. Do not use tools.",
        )
    evidence = client.raw.beta.sessions.retrieve(created.id).model_dump(mode="json")
    assert evidence["agent"]["id"] == config.agent_id
    assert evidence["agent"]["version"] == config.agent_version
    assert evidence["environment_id"] == config.environment_id
