from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def run_checker(name: str, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPOSITORY_ROOT / "scripts" / name), "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def architecture_root(
    tmp_path: Path, handler: str = "from managed_agents_app.runtime import Runtime\n"
) -> Path:
    handlers = tmp_path / "backend/src/managed_agents_app/handlers"
    migrations = tmp_path / "backend/migrations"
    handlers.mkdir(parents=True)
    migrations.mkdir(parents=True)
    (handlers / "example.py").write_text(handler, encoding="utf-8")
    (migrations / "001.sql").write_text("CREATE TABLE safe (id TEXT);\n", encoding="utf-8")
    return tmp_path


def rails_root(tmp_path: Path, limitations: list[dict[str, str]]) -> Path:
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "e2e").mkdir()
    (tmp_path / "knowledge/known-limitations.json").write_text(json.dumps(limitations), encoding="utf-8")
    (tmp_path / "docs/feature-contract-template.md").write_text(
        (REPOSITORY_ROOT / "docs/feature-contract-template.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return tmp_path


def limitation(**updates: str) -> dict[str, str]:
    value = {
        "id": "KL-001",
        "title": "Example limitation",
        "status": "open",
        "owner": "architecture",
        "test": "e2e/example.spec.ts",
        "desired_behavior": "The desired behavior.",
    }
    value.update(updates)
    return value


def test_architecture_checker_accepts_runtime_port_usage(tmp_path):
    result = run_checker("check_architecture.py", architecture_root(tmp_path))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "relative_path, source",
    [
        ("backend/src/managed_agents_app/config.py", "import boto3\nfrom botocore import client\n"),
        ("backend/src/managed_agents_app/events.py", "import boto3\n"),
        ("backend/src/managed_agents_app/operations.py", "import boto3\n"),
        ("backend/src/managed_agents_app/cma_controller/pending_input_s3.py", "import boto3\n"),
        ("backend/src/managed_agents_app/cma_controller/trigger_sqs.py", "import boto3\n"),
        ("backend/src/managed_agents_app/slack/client.py", "import slack_sdk\n"),
        ("backend/src/managed_agents_app/slack/signatures.py", "from slack_sdk import signature\n"),
        ("backend/tests/test_retry_and_blocks.py", "from slack_sdk.errors import SlackApiError\n"),
        ("backend/src/managed_agents_app/managed_agent/client.py", "import anthropic\n"),
        ("backend/src/managed_agents_app/cma_controller/webhook.py", "from anthropic import Anthropic\n"),
    ],
)
def test_architecture_checker_accepts_exact_sdk_allowlist(tmp_path, relative_path, source):
    root = architecture_root(tmp_path)
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("sdk", ["boto3", "botocore", "slack_sdk", "anthropic"])
def test_architecture_checker_rejects_unapproved_sdk_helper(tmp_path, sdk):
    root = architecture_root(tmp_path)
    helper = root / "backend/src/managed_agents_app/helper.py"
    helper.write_text(f"import {sdk}\n", encoding="utf-8")
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 1
    assert f"provider SDK import '{sdk}' is not allowed here" in result.stderr
    assert "helper.py:1" in result.stderr


def test_architecture_checker_rejects_adjacent_unapproved_s3_adapter(tmp_path):
    root = architecture_root(tmp_path)
    helper = root / "backend/src/managed_agents_app/cma_controller/other_s3.py"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("import boto3\n", encoding="utf-8")
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 1
    assert "provider SDK import 'boto3' is not allowed here" in result.stderr


@pytest.mark.parametrize("package", ["a2a_client", "agent_control_plane", "a2a_event_sink", "agui_bridge"])
def test_architecture_checker_rejects_controller_import_from_a2a_application(tmp_path, package):
    root = architecture_root(tmp_path)
    helper = root / f"backend/src/managed_agents_app/{package}/unsafe.py"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("from managed_agents_app.cma_controller import service\n", encoding="utf-8")
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 1
    assert "A2A application code may not import" in result.stderr


def test_architecture_checker_rejects_web_api_provider_access(tmp_path):
    root = architecture_root(tmp_path)
    target = root / "backend/src/managed_agents_app/handlers/web_api.py"
    target.write_text("from managed_agents_app.managed_agent import client\n", encoding="utf-8")
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 1
    assert "Web API may not import 'managed_agents_app.managed_agent'" in result.stderr


@pytest.mark.parametrize("package", ["handlers", "slack"])
def test_architecture_checker_rejects_surface_controller_import(tmp_path, package):
    root = architecture_root(tmp_path)
    target = root / f"backend/src/managed_agents_app/{package}/unsafe.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("from managed_agents_app.cma_controller import service\n", encoding="utf-8")
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 1
    assert "surface code may not import 'managed_agents_app.cma_controller'" in result.stderr


def test_architecture_checker_rejects_legacy_production_path(tmp_path):
    root = architecture_root(tmp_path)
    target = root / "backend/src/managed_agents_app/handlers/example.py"
    target.write_text('def route():\n    return "/api/sessions"\n', encoding="utf-8")
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 1
    assert "forbidden legacy token" in result.stderr


def test_architecture_checker_rejects_frontend_provider_token(tmp_path):
    root = architecture_root(tmp_path)
    target = root / "apps/web/app/assistant.tsx"
    target.parent.mkdir(parents=True)
    target.write_text('export const id = "sesn_old";\n', encoding="utf-8")
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 1
    assert "forbidden legacy token" in result.stderr


def test_architecture_checker_rejects_bridge_runtime_agent_access(tmp_path):
    root = architecture_root(tmp_path)
    target = root / "backend/src/managed_agents_app/agui_bridge/app.py"
    target.parent.mkdir(parents=True)
    target.write_text("def run(runtime):\n    return runtime.agent\n", encoding="utf-8")
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 1
    assert "Web/A2A code may not access Runtime.agent" in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        "from managed_agents_app.managed_agent import ManagedAgentClient\n",
        "from managed_agents_app.cma_controller import service\n",
        "from managed_agents_app.ports.agent import AgentGateway\n",
        "def handle(runtime):\n    return runtime.agent\n",
    ],
)
def test_architecture_checker_rejects_slack_cma_access(tmp_path, source):
    root = architecture_root(tmp_path)
    target = root / "backend/src/managed_agents_app/handlers/agent_input.py"
    target.write_text(source, encoding="utf-8")
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 1
    assert "Slack code may not" in result.stderr


@pytest.mark.parametrize("client", ["ManagedAgentClient", "SlackClient", "WebClient"])
def test_architecture_checker_rejects_handler_client_construction(tmp_path, client):
    source = f"def build():\n    return {client}()\n"
    result = run_checker("check_architecture.py", architecture_root(tmp_path, source))
    assert result.returncode == 1
    assert f"may not construct {client}" in result.stderr
    assert "example.py:2" in result.stderr


@pytest.mark.parametrize(
    "source",
    [
        "from managed_agents_app.managed_agent.client import ManagedAgentClient\n",
        "from managed_agents_app.slack.client import SlackClient\n",
        "from managed_agents_app.testing.fake_agent import FakeManagedAgent\n",
    ],
)
def test_architecture_checker_rejects_port_to_provider_dependency(tmp_path, source):
    root = architecture_root(tmp_path)
    port = root / "backend/src/managed_agents_app/ports/agent.py"
    port.parent.mkdir(parents=True)
    port.write_text(source, encoding="utf-8")
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 1
    assert "ports may not import provider implementation" in result.stderr
    assert "ports/agent.py:1" in result.stderr


def test_architecture_checker_rejects_handler_client_import(tmp_path):
    source = "from managed_agents_app.managed_agent.client import ManagedAgentClient\n"
    result = run_checker("check_architecture.py", architecture_root(tmp_path, source))
    assert result.returncode == 1
    assert "handlers may not import ManagedAgentClient" in result.stderr


def test_architecture_checker_accepts_neutral_model_dependency(tmp_path):
    root = architecture_root(tmp_path)
    port = root / "backend/src/managed_agents_app/ports/agent.py"
    port.parent.mkdir(parents=True)
    port.write_text("from managed_agents_app.models import SessionSummary\n", encoding="utf-8")
    model = root / "backend/src/managed_agents_app/models.py"
    model.write_text("class SessionSummary: ...\n", encoding="utf-8")
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 0, result.stderr


def test_architecture_checker_rejects_neutral_model_to_provider_dependency(tmp_path):
    root = architecture_root(tmp_path)
    model = root / "backend/src/managed_agents_app/models.py"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_text("from managed_agents_app.slack.client import SlackClient\n", encoding="utf-8")
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 1
    assert "neutral modules may not import provider implementation" in result.stderr
    assert "models.py:1" in result.stderr


@pytest.mark.parametrize(
    "source, forbidden",
    [
        ("from managed_agents_app.db.thread_repository import ThreadRepository\n", "db.thread_repository"),
        ("from managed_agents_app.db import thread_repository\n", "db.thread_repository"),
        ("from ..db.thread_repository import ThreadRepository\n", "db.thread_repository"),
        ("from managed_agents_app.slack.client import SlackClient\n", "slack"),
        ("from .. import slack\n", "slack"),
        ("from managed_agents_app.handlers import web_api\n", "handlers"),
        ("from managed_agents_app.testing import fake_agent\n", "testing"),
    ],
)
def test_architecture_checker_rejects_controller_surface_imports(tmp_path, source, forbidden):
    root = architecture_root(tmp_path)
    controller = root / "backend/src/managed_agents_app/cma_controller/example.py"
    controller.parent.mkdir(parents=True)
    controller.write_text(source, encoding="utf-8")
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 1
    assert f"CMA controller may not import 'managed_agents_app.{forbidden}" in result.stderr
    assert "cma_controller/example.py:1" in result.stderr


def test_architecture_checker_allows_controller_provider_adapter(tmp_path):
    root = architecture_root(tmp_path)
    controller = root / "backend/src/managed_agents_app/cma_controller/example.py"
    controller.parent.mkdir(parents=True)
    controller.write_text(
        "from managed_agents_app.managed_agent.client import ManagedAgentClient\n", encoding="utf-8"
    )
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 0, result.stderr


def test_architecture_checker_rejects_transcript_migration_fields(tmp_path):
    root = architecture_root(tmp_path)
    (root / "backend/migrations/002.sql").write_text(
        "ALTER TABLE safe ADD COLUMN message_text TEXT;\n", encoding="utf-8"
    )
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 1
    assert "message_text" in result.stderr
    assert "002.sql:1" in result.stderr


@pytest.mark.parametrize("field", ["user_text", "assistant_text", "task_json", "history_json"])
def test_architecture_checker_rejects_controller_transcript_fields(tmp_path, field):
    root = architecture_root(tmp_path)
    (root / "backend/migrations/002.sql").write_text(
        f"ALTER TABLE cma_tasks ADD COLUMN {field} TEXT;\n", encoding="utf-8"
    )
    result = run_checker("check_architecture.py", root)
    assert result.returncode == 1
    assert field in result.stderr


def test_repository_rails_accept_valid_registry(tmp_path):
    root = rails_root(tmp_path, [limitation()])
    (root / "e2e/example.spec.ts").write_text("// KL-001\n", encoding="utf-8")
    result = run_checker("check_repository_rails.py", root)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "limitations, test_text, expected",
    [
        ([limitation(), limitation()], "// KL-001\n", "duplicate ID KL-001"),
        ([limitation(status="unknown")], "// KL-001\n", "expected one of accepted, open, resolved"),
        ([limitation(test="e2e/missing.spec.ts")], "// KL-001\n", "referenced path does not exist"),
        ([limitation()], "// no annotation\n", "does not reference KL-001"),
    ],
)
def test_repository_rails_reject_invalid_registry(tmp_path, limitations, test_text, expected):
    root = rails_root(tmp_path, limitations)
    (root / "e2e/example.spec.ts").write_text(test_text, encoding="utf-8")
    result = run_checker("check_repository_rails.py", root)
    assert result.returncode == 1
    assert expected in result.stderr


def test_repository_rails_rejects_incomplete_feature_contract_template(tmp_path):
    root = rails_root(tmp_path, [limitation()])
    (root / "e2e/example.spec.ts").write_text("// KL-001\n", encoding="utf-8")
    (root / "docs/feature-contract-template.md").write_text("# Feature Contract — <name>\n")
    result = run_checker("check_repository_rails.py", root)
    assert result.returncode == 1
    assert "missing heading: ## Goal" in result.stderr
