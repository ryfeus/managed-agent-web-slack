#!/usr/bin/env python3
"""Local-only real Slack user commands and deployed-path smoke test."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
SLACK_API = "https://slack.com/api"
POLL_INTERVALS = (1, 2, 5)


class SlackUserError(Exception):
    """A local command failed without exposing credentials."""


class SmokeFailure(SlackUserError):
    def __init__(self, boundary: str, evidence: str) -> None:
        self.boundary = boundary
        self.evidence = evidence
        super().__init__(evidence)


def redact(value: str) -> str:
    for name in ("SLACK_USER_TOKEN", "SLACK_BOT_TOKEN"):
        token = os.getenv(name)
        if token:
            value = value.replace(token, "[REDACTED]")
    return value


def emit(value: dict[str, Any]) -> None:
    print(redact(json.dumps(value, indent=2)))


def required_env(name: str, boundary: str = "configuration") -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SmokeFailure(boundary, f"Set {name} in the local environment or .env")
    return value


class SlackApi:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def call(self, token: str, method: str, data: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            response = self.client.post(
                f"{SLACK_API}/{method}",
                headers={"Authorization": f"Bearer {token}"},
                data=data or {},
            )
        except httpx.HTTPError as error:
            raise SlackUserError(f"Slack {method} transport error: {type(error).__name__}") from None
        try:
            result = response.json()
        except ValueError:
            raise SlackUserError(
                f"Slack {method} returned invalid JSON (HTTP {response.status_code})"
            ) from None
        if not isinstance(result, dict):
            raise SlackUserError(f"Slack {method} returned a non-object response")
        if result.get("ok") is not True:
            code = redact(str(result.get("error", "unknown_error")))[:100]
            raise SlackUserError(f"Slack {method} failed: {code}")
        if response.status_code >= 400:
            raise SlackUserError(f"Slack {method} failed: HTTP {response.status_code}")
        return result

    def auth(self, token: str) -> dict[str, Any]:
        result = self.call(token, "auth.test")
        if not result.get("team_id") or not result.get("user_id"):
            raise SlackUserError("Slack auth.test omitted team_id or user_id")
        return result

    def post(self, token: str, channel: str, text: str, thread_ts: str | None = None) -> dict[str, str]:
        data = {"channel": channel, "text": text}
        if thread_ts:
            data["thread_ts"] = thread_ts
        result = self.call(token, "chat.postMessage", data)
        if not result.get("ts"):
            raise SlackUserError("Slack chat.postMessage omitted ts")
        if result.get("channel") and result["channel"] != channel:
            raise SlackUserError("Slack chat.postMessage returned a different channel")
        ts = str(result["ts"])
        return {"channel": channel, "ts": ts, "thread_ts": thread_ts or ts}

    def thread(self, token: str, channel: str, thread_ts: str) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        for _ in range(20):
            data = {"channel": channel, "ts": thread_ts, "limit": "100"}
            if cursor:
                data["cursor"] = cursor
            result = self.call(token, "conversations.replies", data)
            page = result.get("messages")
            if not isinstance(page, list):
                raise SlackUserError("Slack conversations.replies omitted messages")
            messages.extend(normalize_message(item, thread_ts) for item in page)
            if not result.get("has_more"):
                break
            cursor = str((result.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor or cursor in seen_cursors:
                raise SlackUserError("Slack conversations.replies pagination did not advance")
            seen_cursors.add(cursor)
        else:
            raise SlackUserError("Slack conversations.replies exceeded 20 pages")
        return {"channel": channel, "thread_ts": thread_ts, "messages": messages}


def visible_block_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := visible_block_text(item)))
    if not isinstance(value, dict):
        return ""
    parts: list[str] = []
    for key, item in value.items():
        if key in {"text", "markdown_text"} and isinstance(item, str):
            parts.append(item)
        elif isinstance(item, (dict, list)):
            nested = visible_block_text(item)
            if nested:
                parts.append(nested)
    return "\n".join(parts)


def normalize_message(value: Any, root_ts: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value.get("ts"):
        raise SlackUserError("Slack conversations.replies contained a message without ts")
    text = value.get("text") if isinstance(value.get("text"), str) else ""
    blocks = visible_block_text(value.get("blocks", []))
    chunks = visible_block_text(value.get("chunks", []))
    attachments = visible_block_text(value.get("attachments", []))
    markdown = value.get("markdown_text") if isinstance(value.get("markdown_text"), str) else ""
    alternate = "\n".join(part for part in (blocks, chunks, attachments, markdown) if part)
    return {
        "ts": str(value["ts"]),
        "user": value.get("user"),
        "bot_id": value.get("bot_id"),
        "text": text,
        "content": text or alternate,
        "block_text": alternate,
        "thread_ts": str(value.get("thread_ts") or root_ts),
    }


def marker_matches(
    messages: list[dict[str, Any]],
    marker: str,
    root_ts: str,
    human_user_id: str,
    agent_user_id: str,
    agent_bot_id: str | None,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for message in messages:
        if message["ts"] == root_ts or message["thread_ts"] != root_ts:
            continue
        if message.get("user") == human_user_id:
            continue
        if message.get("user") != agent_user_id and (
            not agent_bot_id or message.get("bot_id") != agent_bot_id
        ):
            continue
        text = str(message.get("text") or "")
        block_text = str(message.get("block_text") or "")
        content = text if marker in text else block_text
        if marker in content:
            if content.count(marker) != 1:
                raise SmokeFailure("slack_projection", f"Agent message repeats {marker}")
            matches.append(message)
    if len({message["ts"] for message in matches}) != len(matches):
        raise SmokeFailure("thread_read", f"Slack returned a duplicate message timestamp for {marker}")
    if len(matches) > 1:
        raise SmokeFailure("slack_projection", f"More than one agent message contains {marker}")
    return matches


def inspect_binding(team_id: str, channel: str, root_ts: str) -> dict[str, Any]:
    command = [
        sys.executable,
        str(ROOT / "scripts/live_a2a_inspect.py"),
        "--team-id",
        team_id,
        "--channel-id",
        channel,
        "--thread-ts",
        root_ts,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT / "backend",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SmokeFailure("backend_inspection", f"Inspector could not run: {type(error).__name__}") from None
    if result.returncode:
        raise SmokeFailure("backend_inspection", f"Inspector exited with status {result.returncode}")
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise SmokeFailure("backend_inspection", "Inspector returned invalid JSON") from None
    if not isinstance(value, dict):
        raise SmokeFailure("backend_inspection", "Inspector returned a non-object result")
    return value


def validate_inspection(value: dict[str, Any], team_id: str) -> dict[str, Any]:
    binding = value.get("binding")
    if not isinstance(binding, dict):
        raise SmokeFailure("slack_delivery", "No Slack application-thread binding was found")
    if not binding.get("thread_id") or not binding.get("context_id") or binding.get("agent_id") != "cma":
        raise SmokeFailure("continuation_binding", "Binding has no application thread/CMA context")
    if value.get("legacy_tables_present") != []:
        raise SmokeFailure("continuation_binding", "Legacy application-session tables remain")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 2:
        count = len(tasks) if isinstance(tasks, list) else "none"
        raise SmokeFailure("a2a_task_creation", f"Expected two tasks, found {count}")
    task_ids = [task.get("task_id") for task in tasks if isinstance(task, dict)]
    message_ids = [task.get("client_message_id") for task in tasks if isinstance(task, dict)]
    if len(task_ids) != 2 or not all(task_ids) or len(set(task_ids)) != 2:
        raise SmokeFailure("a2a_task_creation", "Task IDs are missing or duplicated")
    if (
        len(message_ids) != 2
        or len(set(message_ids)) != 2
        or not all(
            isinstance(item, str) and item.startswith(f"slack:event:{team_id}:") for item in message_ids
        )
    ):
        raise SmokeFailure("a2a_task_creation", "Tasks are not two distinct Slack-originated turns")
    if any(task["controller_state"] != "COMPLETED" for task in tasks):
        raise SmokeFailure("agent_execution", "A2A Tasks have not both completed")
    if any(task["projection_status"] != "completed" or task["task_state"] != "COMPLETED" for task in tasks):
        raise SmokeFailure("slack_projection", "Slack projections have not both completed")
    return value


def timeout_boundary(value: dict[str, Any], expected_tasks: int) -> str:
    if not value.get("binding"):
        return "slack_delivery"
    tasks = value.get("tasks") or []
    if len(tasks) < expected_tasks:
        return "continuation_binding" if expected_tasks == 2 else "a2a_task_creation"
    task = tasks[expected_tasks - 1]
    if task.get("controller_state") != "COMPLETED":
        return "agent_execution"
    if task.get("projection_status") != "completed":
        return "slack_projection"
    return "thread_read"


def wait_for_marker(
    api: SlackApi,
    token: str,
    channel: str,
    root_ts: str,
    marker: str,
    human_user_id: str,
    agent_user_id: str,
    agent_bot_id: str | None,
    team_id: str,
    expected_tasks: int,
    *,
    timeout: float = 180,
    clock: Any = time.monotonic,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    deadline = clock() + timeout
    attempt = 0
    while True:
        try:
            thread = api.thread(token, channel, root_ts)
        except SlackUserError as error:
            raise SmokeFailure("thread_read", str(error)) from None
        matches = marker_matches(
            thread["messages"], marker, root_ts, human_user_id, agent_user_id, agent_bot_id
        )
        if matches:
            return matches[0]
        remaining = deadline - clock()
        if remaining <= 0:
            state = inspect_binding(team_id, channel, root_ts)
            raise SmokeFailure(timeout_boundary(state, expected_tasks), f"Timed out waiting for {marker}")
        sleep(min(POLL_INTERVALS[min(attempt, len(POLL_INTERVALS) - 1)], remaining))
        attempt += 1


def wait_for_inspection(team_id: str, channel: str, root_ts: str, *, timeout: float = 90) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        state = inspect_binding(team_id, channel, root_ts)
        try:
            return validate_inspection(state, team_id)
        except SmokeFailure as error:
            if time.monotonic() >= deadline:
                raise error
            time.sleep(2)


def smoke(api: SlackApi) -> dict[str, Any]:
    report: dict[str, Any] = {"run_id": uuid4().hex[:12]}
    try:
        user_token = required_env("SLACK_USER_TOKEN", "user_auth")
        bot_token = required_env("SLACK_BOT_TOKEN", "bot_auth")
        channel = required_env("E2E_LIVE_SLACK_CHANNEL_ID")
        required_env("DSQL_ENDPOINT", "backend_inspection")
        try:
            human = api.auth(user_token)
        except SlackUserError as error:
            raise SmokeFailure("user_auth", str(error)) from None
        try:
            agent = api.auth(bot_token)
        except SlackUserError as error:
            raise SmokeFailure("bot_auth", str(error)) from None
        team_id = str(human["team_id"])
        expected_team = os.getenv("E2E_LIVE_SLACK_TEAM_ID", "").strip()
        if agent["team_id"] != team_id or (expected_team and expected_team != team_id):
            raise SmokeFailure("user_auth", "User, bot, and configured team IDs do not match")
        human_id = str(human["user_id"])
        agent_id = str(agent["user_id"])
        bot_id = str(agent["bot_id"]) if agent.get("bot_id") else None
        if human_id == agent_id:
            raise SmokeFailure("bot_auth", "User and bot tokens resolved to the same Slack user")
        report.update(
            team_id=team_id,
            human_user_id=human_id,
            agent_user_id=agent_id,
            channel_id=channel,
        )
        first = f"ACK_{report['run_id']}"
        second = f"CONTINUE_{report['run_id']}"
        report.update(first_marker=first, continuation_marker=second)
        try:
            posted = api.post(
                user_token, channel, f"<@{agent_id}> [E2E {report['run_id']}] Reply exactly {first}"
            )
        except SlackUserError as error:
            raise SmokeFailure("human_post", str(error)) from None
        root_ts = posted["thread_ts"]
        report["thread_ts"] = root_ts
        wait_for_marker(api, user_token, channel, root_ts, first, human_id, agent_id, bot_id, team_id, 1)
        try:
            continuation = api.post(
                user_token, channel, f"[E2E {report['run_id']}] Reply exactly {second}", root_ts
            )
        except SlackUserError as error:
            raise SmokeFailure("human_post", str(error)) from None
        if continuation["thread_ts"] != root_ts:
            raise SmokeFailure("continuation_binding", "Slack returned a different root for the second turn")
        wait_for_marker(api, user_token, channel, root_ts, second, human_id, agent_id, bot_id, team_id, 2)
        state = wait_for_inspection(team_id, channel, root_ts)
        try:
            final_thread = api.thread(user_token, channel, root_ts)
        except SlackUserError as error:
            raise SmokeFailure("thread_read", str(error)) from None
        for marker in (first, second):
            matches = marker_matches(final_thread["messages"], marker, root_ts, human_id, agent_id, bot_id)
            if len(matches) != 1:
                raise SmokeFailure("slack_projection", f"Final thread does not contain exactly one {marker}")
        report.update(
            application_thread_id=state["binding"]["thread_id"],
            context_id=state["binding"]["context_id"],
            task_ids=[task["task_id"] for task in state["tasks"]],
            push_receipts=[task["push_receipts"] for task in state["tasks"]],
        )
        return report
    except SmokeFailure as error:
        report.update(first_broken_boundary=error.boundary, evidence=redact(error.evidence))
        raise SmokeFailure(error.boundary, json.dumps(report, indent=2)) from None


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("whoami")
    post = commands.add_parser("post")
    post.add_argument("--channel", required=True)
    post.add_argument("--text", required=True)
    post.add_argument("--thread-ts")
    thread = commands.add_parser("thread")
    thread.add_argument("--channel", required=True)
    thread.add_argument("--thread-ts", required=True)
    commands.add_parser("smoke")
    args = parser.parse_args()
    with httpx.Client(timeout=10) as client:
        api = SlackApi(client)
        try:
            if args.command == "whoami":
                auth = api.auth(required_env("SLACK_USER_TOKEN", "user_auth"))
                emit({key: auth.get(key) for key in ("team_id", "user_id", "user", "team")})
            elif args.command == "post":
                emit(api.post(required_env("SLACK_USER_TOKEN"), args.channel, args.text, args.thread_ts))
            elif args.command == "thread":
                emit(api.thread(required_env("SLACK_USER_TOKEN"), args.channel, args.thread_ts))
            else:
                report = smoke(api)
                print("Slack user E2E smoke: PASS")
                emit(report)
        except SmokeFailure as error:
            if args.command == "smoke":
                print("Slack user E2E smoke: FAIL", file=sys.stderr)
            print(redact(str(error)), file=sys.stderr)
            return 1
        except SlackUserError as error:
            print(redact(str(error)), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
