from __future__ import annotations

import json

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
