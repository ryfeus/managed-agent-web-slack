"""Explicit live MCP acceptance using a disposable Cognito user and the official SDK harness."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aurora_dsql_psycopg as dsql
import boto3
import httpx
from mcp_admin import link_identity

ROOT = Path(__file__).resolve().parents[1]


def cli(*command: str) -> dict:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed; verify credentials and deployment")
    return json.loads(result.stdout)


def main() -> None:
    if os.getenv("RUN_LIVE_MCP") != "1":
        raise RuntimeError("Set RUN_LIVE_MCP=1 for the explicitly authorized live test")
    profile = os.getenv("AWS_PROFILE", "default")
    account = cli("aws", "sts", "get-caller-identity", "--profile", profile)["Account"]
    if account != os.getenv("EXPECTED_AWS_ACCOUNT_ID"):
        raise RuntimeError("Set EXPECTED_AWS_ACCOUNT_ID to the target sandbox account")
    credentials = cli("aws", "configure", "export-credentials", "--profile", profile, "--format", "process")
    for target, source in (
        ("AWS_ACCESS_KEY_ID", "AccessKeyId"),
        ("AWS_SECRET_ACCESS_KEY", "SecretAccessKey"),
        ("AWS_SESSION_TOKEN", "SessionToken"),
    ):
        os.environ[target] = credentials[source]
    region = "us-west-2"
    outputs = {
        key: item["value"] for key, item in cli("terraform", "-chdir=infra/app", "output", "-json").items()
    }
    url, issuer, pool = (outputs[key] for key in ("mcp_url", "mcp_oauth_issuer", "mcp_user_pool_id"))
    role = outputs["mcp_runtime_role_arn"]
    if role in outputs["dsql_runtime_role_arns"]:
        raise RuntimeError("MCP role must not have the broad application binding")
    cognito = boto3.client("cognito-idp", region_name=region)
    client_id = outputs["mcp_client_ids"]["codex"]
    client = cognito.describe_user_pool_client(UserPoolId=pool, ClientId=client_id)["UserPoolClient"]
    if client.get("ClientSecret") or client["AllowedOAuthFlows"] != ["code"]:
        raise RuntimeError("Expected a public authorization-code client")
    redirect = next(value for value in client["CallbackURLs"] if value.startswith("http://127.0.0.1:"))
    stack = boto3.client("cloudformation", region_name=region).describe_stacks(
        StackName=role.rsplit("/", 1)[-1]
    )["Stacks"][0]
    origin = next(item["OutputValue"] for item in stack["Outputs"] if item["OutputKey"] == "EndpointDomain")

    def database():
        return dsql.connect(
            host=outputs["dsql_endpoint"],
            region=region,
            user="admin",
            dbname="postgres",
            sslmode="require",
            connect_timeout=15,
        )

    report: dict = {
        "timestamp": datetime.now(UTC).isoformat(),
        "account": account,
        "url": url,
        "checks": [],
        "threads": [],
        "tasks": [],
    }

    def check(name: str) -> None:
        report["checks"].append(name)
        print(f"PASS {name}", flush=True)

    def harness(config: dict) -> dict:
        print("Running MCP harness phase: " + config["phase"], flush=True)
        try:
            result = subprocess.run(
                [str(ROOT / "node_modules/.bin/node"), "--import", "tsx", "e2e/helpers/mcp-live-harness.ts"],
                cwd=ROOT,
                input=json.dumps(config),
                capture_output=True,
                text=True,
                timeout=480,
                check=False,
            )
            output, failed = result.stdout, result.returncode != 0
        except subprocess.TimeoutExpired as error:
            raw = error.stdout or b""
            output = raw.decode() if isinstance(raw, bytes) else raw
            failed = True
            report["failure_stage"] = "harness_timeout"
        tokens = {}
        for line in output.splitlines():
            event = json.loads(line)
            if event["event"] == "tokens":
                tokens = event
            elif event["event"] == "thread":
                report["threads"].append(event["threadId"])
            elif event["event"] == "task":
                report["tasks"].append(
                    {key: event[key] for key in ("threadId", "taskId", "requestId", "state")}
                )
            elif event["event"] == "state":
                for task in report["tasks"]:
                    if task["taskId"] == event["taskId"]:
                        task["state"] = event["state"]
            elif event["event"] == "check":
                check(event["name"])
            elif event["event"] == "failure":
                report["failure_stage"] = event["stage"]
        if failed:
            raise RuntimeError("MCP harness failed at " + report.get("failure_stage", "startup"))
        return tokens

    with httpx.Client(timeout=30) as http:
        metadata = http.get(url.removesuffix("/mcp") + "/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200 and metadata.json()["resource"] == url
        assert http.post(url, json={}).status_code == 401
        check("resource_discovery_and_unauthenticated_denial")
        assert http.get("https://" + origin + "/mcp").status_code == 403
        check("direct_origin_denied")

    check("runtime_role_excluded_from_broad_application_grants")
    with database() as conn:
        mappings = conn.execute(
            "SELECT pg_role_name FROM sys.iam_pg_role_mappings WHERE arn=%s", (role,)
        ).fetchall()
        assert mappings == [("mcp_runtime",)]
        grants = conn.execute(
            "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee='mcp_runtime'"
        ).fetchall()
        expected = {
            (table, privilege)
            for table in ("agent_threads", "agent_tasks")
            for privilege in ("SELECT", "INSERT", "UPDATE")
        }
        assert set(grants) == expected | {("external_identities", "SELECT")}
        check("actual_database_grants_are_restricted")
        row = conn.execute(
            "SELECT principal_id FROM external_identities WHERE provider='web' "
            "AND tenant_id='development' AND external_user_id='single-user'"
        ).fetchone()
        if not row:
            raise RuntimeError("Existing Web principal mapping is missing")
        principal = str(row[0])

    nonce = uuid.uuid4().hex
    username, password = f"mcp-live-{nonce}@example.invalid", "Aa1!" + secrets.token_urlsafe(32)
    subject, tokens = None, {}
    foreign_owner, foreign_thread = str(uuid.uuid4()), str(uuid.uuid4())
    user_created = False
    config = {
        "url": url,
        "issuer": issuer,
        "clientId": client_id,
        "redirectUri": redirect,
        "marker": "MCP_LIVE_OK_" + nonce[:8],
    }
    try:
        cognito.admin_create_user(
            UserPoolId=pool,
            Username=username,
            MessageAction="SUPPRESS",
            TemporaryPassword=password,
            UserAttributes=[
                {"Name": "email", "Value": username},
                {"Name": "email_verified", "Value": "true"},
            ],
        )
        user_created = True
        cognito.admin_set_user_password(UserPoolId=pool, Username=username, Password=password, Permanent=True)
        user = cognito.admin_get_user(UserPoolId=pool, Username=username)
        subject = next(item["Value"] for item in user["UserAttributes"] if item["Name"] == "sub")
        tokens = harness({**config, "phase": "oauth", "username": username, "password": password})
        check("cognito_pkce_login_and_refresh")
        headers = {"Authorization": "Bearer " + tokens["accessToken"]}
        with httpx.Client(timeout=30) as http:
            assert http.post(url, headers=headers, json={}).status_code == 403
            check("unlinked_identity_denied")
            assert (
                http.post(url, headers={"Authorization": "Bearer " + tokens["idToken"]}, json={}).status_code
                == 401
            )
            check("id_token_denied")
        with database() as conn:
            link_identity(conn, issuer, subject, principal)
            conn.execute("INSERT INTO principals (principal_id) VALUES (%s)", (foreign_owner,))
            conn.execute(
                "INSERT INTO agent_threads (thread_id,principal_id,agent_id,creation_request_id) "
                "VALUES (%s,%s,'cma',%s)",
                (foreign_thread, foreign_owner, str(uuid.uuid4())),
            )
        with httpx.Client(timeout=30) as http:
            response = http.get(
                url, headers={**headers, "Accept": "text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
            )
            report["authenticated_get"] = {
                "status": response.status_code,
                "cache_control": response.headers.get("cache-control"),
            }
            assert response.status_code in (200, 400, 405, 406)
            assert response.headers.get("cache-control") == "no-store"
            check("cloudfront_get_preserves_authorization")
        harness(
            {
                **config,
                "phase": "tools",
                "accessToken": tokens["accessToken"],
                "foreignThreadId": foreign_thread,
            }
        )
        with database() as conn:
            for task in report["tasks"]:
                rows = conn.execute(
                    "SELECT task_id FROM agent_tasks WHERE thread_id=%s AND client_message_id=%s",
                    (task["threadId"], "mcp:" + task["requestId"]),
                ).fetchall()
                assert len(rows) == 1 and str(rows[0][0]) == task["taskId"]
        check("durable_task_binding_uniqueness")
        web_token = boto3.client("secretsmanager", region_name=region).get_secret_value(
            SecretId=outputs["web_access_token_secret_arn"]
        )["SecretString"]
        with httpx.Client(timeout=30) as http:
            base = outputs["application_url"]
            assert http.post(base + "/api/auth/session", json={"accessToken": web_token}).status_code == 200
            response = http.get(base + "/api/agui/threads/" + report["threads"][0] + "/history")
            assert response.status_code == 200
            assert any(
                message.get("role") == "assistant" and config["marker"] in message.get("content", "")
                for message in response.json()["messages"]
            )
        check("shared_web_canonical_history")
        report["ok"] = True
    finally:
        failures = []

        def cleanup(name, action):
            try:
                action()
            except Exception:
                failures.append(name)

        def settle_threads():
            if report.get("ok") or not report["threads"]:
                return
            # An uncertain MCP response can follow admission. Web Stop reconciles
            # the same owned thread even when no Task ID reached the harness.
            access = boto3.client("secretsmanager", region_name=region).get_secret_value(
                SecretId=outputs["web_access_token_secret_arn"]
            )["SecretString"]
            with httpx.Client(timeout=60) as http:
                base = outputs["application_url"]
                assert http.post(base + "/api/auth/session", json={"accessToken": access}).status_code == 200
                for thread in report["threads"]:
                    assert http.post(base + "/api/agui/threads/" + thread + "/cancel").status_code == 200

        def unlink_and_archive():
            # Retain canonical execution and binding evidence; archive owned test threads.
            with database() as conn:
                if subject:
                    conn.execute(
                        "DELETE FROM external_identities WHERE provider='cognito' "
                        "AND tenant_id=%s AND external_user_id=%s",
                        (issuer, subject),
                    )
                for thread in report["threads"]:
                    conn.execute(
                        "UPDATE agent_threads SET archived_at=CURRENT_TIMESTAMP "
                        "WHERE thread_id=%s AND principal_id=%s",
                        (thread, principal),
                    )
                conn.execute(
                    "DELETE FROM agent_threads WHERE thread_id=%s AND principal_id=%s",
                    (foreign_thread, foreign_owner),
                )
                conn.execute("DELETE FROM principals WHERE principal_id=%s", (foreign_owner,))

        def revoke_tokens():
            if tokens:
                with httpx.Client(timeout=30) as http:
                    response = http.post(
                        url, headers={"Authorization": "Bearer " + tokens["accessToken"]}, json={}
                    )
                    assert response.status_code == 403
                    check("unlink_revokes_application_access")
                    response = http.post(
                        tokens["revokeUrl"], data={"client_id": client_id, "token": tokens["refreshToken"]}
                    )
                    assert response.status_code == 200

        cleanup("stop_pending_tasks", settle_threads)
        cleanup("unlink_and_archive", unlink_and_archive)
        cleanup("revoke_tokens", revoke_tokens)
        if user_created:
            cleanup("delete_test_user", lambda: cognito.admin_delete_user(UserPoolId=pool, Username=username))
        report["test_identity_removed"] = "delete_test_user" not in failures
        report["cleanup_failures"] = failures
        if failures:
            report["ok"] = False
            report["cleanup_identity"] = {"username": username, "subject": subject}
        destination = ROOT / ".generated/mcp-live-report.json"
        destination.write_text(json.dumps(report, indent=2) + "\n")
        print("Metadata-only report: " + str(destination), flush=True)
        if failures:
            raise RuntimeError("Live harness cleanup requires attention")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # AWS/HTTP exception details can contain credentials, URLs or wire bodies.
        print(
            "Live MCP acceptance failed: "
            + type(error).__name__
            + " sqlstate="
            + str(getattr(error, "sqlstate", None)),
            file=sys.stderr,
        )
        sys.exit(1)
