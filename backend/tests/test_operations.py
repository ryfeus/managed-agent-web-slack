from __future__ import annotations

import json

import pytest

from managed_agents_app import operations


class Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _statement, _parameters=None):
        return None

    def fetchone(self):
        return None


class Connection:
    autocommit = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return Cursor()


def test_migrations_dispatch_each_statement_independently(monkeypatch, tmp_path, config) -> None:
    migration = tmp_path / "001_test.sql"
    migration.write_text(
        "CREATE TABLE IF NOT EXISTS one (id TEXT);\nCREATE TABLE IF NOT EXISTS two (id TEXT);\n"
    )
    executed: list[tuple[str, tuple[object, ...]]] = []

    monkeypatch.setattr(operations, "MIGRATIONS", tmp_path)
    monkeypatch.setattr(operations, "load_config", lambda: config)
    monkeypatch.setattr(operations, "connect", lambda *_args: Connection())
    monkeypatch.setattr(
        operations,
        "_execute_admin",
        lambda _config, statement, parameters=(): executed.append((statement, parameters)),
    )

    operations.migrate()

    assert [statement for statement, _ in executed[1:-1]] == [
        "CREATE TABLE IF NOT EXISTS one (id TEXT)",
        "CREATE TABLE IF NOT EXISTS two (id TEXT)",
    ]
    assert executed[-1][0].startswith("INSERT INTO schema_migrations")
    assert executed[-1][1] == ("001_test.sql",)


def test_failed_drop_does_not_mark_migration_applied(monkeypatch, tmp_path, config) -> None:
    (tmp_path / "008_test.sql").write_text("DROP TABLE IF EXISTS child;\nDROP TABLE IF EXISTS parent;\n")
    executed: list[str] = []

    def execute(_config, statement, _parameters=()):
        executed.append(statement)
        if statement == "DROP TABLE IF EXISTS parent":
            raise RuntimeError("dependent object")

    monkeypatch.setattr(operations, "MIGRATIONS", tmp_path)
    monkeypatch.setattr(operations, "load_config", lambda: config)
    monkeypatch.setattr(operations, "connect", lambda *_args: Connection())
    monkeypatch.setattr(operations, "_execute_admin", execute)
    with pytest.raises(RuntimeError, match="dependent object"):
        operations.migrate()
    assert executed == [
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)",
        "DROP TABLE IF EXISTS child",
        "DROP TABLE IF EXISTS parent",
    ]


def test_dsql_index_uses_async_ddl_and_waits_before_migration_marker(monkeypatch, config) -> None:
    executed: list[str] = []
    waited: list[tuple[str, str | None]] = []

    class IndexCursor(Cursor):
        description = True

        def execute(self, statement, _parameters=None):
            executed.append(statement)

        def fetchone(self):
            return {"job_id": "job-1"}

    class IndexConnection(Connection):
        def cursor(self):
            return IndexCursor()

    monkeypatch.setattr(operations, "connect", lambda *_args: IndexConnection())
    monkeypatch.setattr(
        operations,
        "_wait_for_dsql_index",
        lambda _config, index_name, job_id: waited.append((index_name, job_id)),
    )
    operations._execute_migration_statement(
        config.model_copy(update={"database_mode": "dsql"}),
        "CREATE UNIQUE INDEX IF NOT EXISTS agent_threads_creation_request_unique "
        "ON agent_threads (principal_id, creation_request_id)",
    )

    assert executed == [
        "CREATE UNIQUE INDEX ASYNC IF NOT EXISTS agent_threads_creation_request_unique "
        "ON agent_threads (principal_id, creation_request_id)"
    ]
    assert waited == [("agent_threads_creation_request_unique", "job-1")]


def test_failed_dsql_index_job_cannot_complete_migration(monkeypatch, config) -> None:
    class JobCursor(Cursor):
        def execute(self, _statement, _parameters=None):
            pass

        def fetchone(self):
            return {"status": "failed", "details": "duplicate key"}

    class JobConnection(Connection):
        def cursor(self):
            return JobCursor()

    monkeypatch.setattr(operations, "connect", lambda *_args: JobConnection())
    with pytest.raises(RuntimeError, match="duplicate key"):
        operations._wait_for_dsql_index(config, "agent_threads_creation_request_unique", "job-1")


def test_existing_dsql_index_must_be_valid_on_retry(monkeypatch, config) -> None:
    valid = False

    class CatalogCursor(Cursor):
        def execute(self, statement, parameters=None):
            assert "pg_index" in statement
            assert parameters == ("agent_threads_creation_request_unique",)

        def fetchone(self):
            return {"valid": valid}

    class CatalogConnection(Connection):
        def cursor(self):
            return CatalogCursor()

    monkeypatch.setattr(operations, "connect", lambda *_args: CatalogConnection())
    with pytest.raises(RuntimeError, match="not valid"):
        operations._wait_for_dsql_index(config, "agent_threads_creation_request_unique", None)
    valid = True
    operations._wait_for_dsql_index(config, "agent_threads_creation_request_unique", None)


def test_sync_secrets_includes_web_credentials(monkeypatch) -> None:
    class SecretsManager:
        def __init__(self) -> None:
            self.values: list[tuple[str, str]] = []

        def put_secret_value(self, *, SecretId, SecretString) -> None:
            self.values.append((SecretId, SecretString))

    client = SecretsManager()
    outputs = {
        name: {"value": f"arn:aws:secretsmanager:us-west-2:123456789012:secret:{name}"}
        for name in (
            "anthropic_api_key_secret_arn",
            "anthropic_webhook_signing_key_secret_arn",
            "slack_signing_secret_arn",
            "slack_bot_token_secret_arn",
            "web_access_token_secret_arn",
            "web_cookie_secret_secret_arn",
        )
    }
    monkeypatch.setattr(
        operations.subprocess, "check_output", lambda *_args, **_kwargs: json.dumps(outputs).encode()
    )
    monkeypatch.setattr(operations.boto3, "client", lambda *_args, **_kwargs: client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic")
    monkeypatch.setenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", "webhook")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "slack-signing")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack-token")
    monkeypatch.setenv("WEB_ACCESS_TOKEN", "web-token")
    monkeypatch.setenv("WEB_COOKIE_SECRET", "cookie")

    operations.sync_secrets()

    assert {secret_id.rsplit(":", 1)[-1] for secret_id, _ in client.values} == {
        "anthropic_api_key_secret_arn",
        "anthropic_webhook_signing_key_secret_arn",
        "slack_signing_secret_arn",
        "slack_bot_token_secret_arn",
        "web_access_token_secret_arn",
        "web_cookie_secret_secret_arn",
    }
