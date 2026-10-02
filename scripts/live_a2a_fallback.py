#!/usr/bin/env python3
"""Exercise deployed Slack stream fallback on an isolated bot-created binding."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid

from managed_agents_app.config import load_config
from managed_agents_app.db.connection import connect
from managed_agents_app.db.slack_a2a_repository import SlackA2ARepository
from managed_agents_app.db.thread_repository import ThreadRepository
from managed_agents_app.domain import (
    A2A_TASK_UPDATED,
    SLACK_TASK_PROJECTION_REQUESTED,
    A2ATaskUpdated,
    SlackTaskProjectionRequested,
)
from managed_agents_app.events import EventBridgeEventBus
from managed_agents_app.slack.client import SlackClient


def wait_projection(repository: SlackA2ARepository, binding_id: str, task_id: str, status: str) -> dict:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        projection = repository.get_projection(binding_id, task_id)
        if projection and projection["status"] == status:
            return projection
        time.sleep(2)
    raise TimeoutError(f"Slack projection did not reach {status}")


def main() -> None:
    if os.getenv("RUN_LIVE_FALLBACK") != "1":
        raise SystemExit("Set RUN_LIVE_FALLBACK=1 for this dev sandbox test")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--team-id", required=True)
    parser.add_argument("--channel-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--event-bus", required=True)
    args = parser.parse_args()

    config = load_config()
    if not config.slack_bot_token:
        raise RuntimeError("Slack bot token is required")
    threads = ThreadRepository(config, "admin")
    thread = threads.get_thread(args.thread_id)
    task_binding = threads.get_task_binding("cma", args.task_id)
    if not thread or not task_binding or str(task_binding["thread_id"]) != args.thread_id:
        raise ValueError("The Task must belong to the selected application thread")
    if thread["agent_id"] != "cma" or not thread["context_id"] or thread["archived_at"]:
        raise ValueError("The selected CMA thread must have an active context")
    with connect(config, "admin") as conn:
        identity = conn.execute(
            "SELECT principal_id FROM external_identities WHERE provider = 'slack' "
            "AND tenant_id = %s AND external_user_id = %s",
            (args.team_id, args.user_id),
        ).fetchone()
        task = conn.execute("SELECT a2a_state FROM cma_tasks WHERE task_id = %s", (args.task_id,)).fetchone()
    if not identity or str(identity["principal_id"]) != str(thread["principal_id"]):
        raise ValueError("Slack identity does not own the selected thread")
    if not task or task["a2a_state"] != "COMPLETED":
        raise ValueError("The selected Task must be completed")

    slack = SlackClient(config.slack_bot_token)
    fixture = slack.raw.chat_postMessage(
        channel=args.channel_id,
        text=f"Phase 4 fallback fixture {uuid.uuid4()}",
        unfurl_links=False,
        unfurl_media=False,
    )
    if not fixture.get("ok") or not fixture.get("ts"):
        raise RuntimeError("Could not create the Slack fixture root")
    root_ts = str(fixture["ts"])
    binding = threads.bind_surface(args.thread_id, "slack", args.team_id, f"{args.channel_id}:{root_ts}")
    binding_id = str(binding["binding_id"])
    bus = EventBridgeEventBus(args.event_bus)
    request_id = f"fallback-fixture:{uuid.uuid4()}"
    bus.publish(
        "app.slack",
        SLACK_TASK_PROJECTION_REQUESTED,
        SlackTaskProjectionRequested(
            request_id=request_id,
            thread_id=str(uuid.uuid4()),  # A known-invalid thread forces subscription failure.
            task_id=args.task_id,
            binding_id=binding_id,
            team_id=args.team_id,
            channel_id=args.channel_id,
            thread_ts=root_ts,
            user_id=args.user_id,
            source_message_ts=root_ts,
        ),
    )
    projections = SlackA2ARepository(config, "admin")
    fallback = wait_projection(projections, binding_id, args.task_id, "fallback")
    if not fallback["slack_message_ts"]:
        raise AssertionError("Fallback did not retain the early Slack task card")
    card_ts = str(fallback["slack_message_ts"])
    bus.publish(
        "app.a2a",
        A2A_TASK_UPDATED,
        A2ATaskUpdated(
            delivery_id=f"fallback-recovery:{uuid.uuid4()}",
            agent_id="cma",
            task_id=args.task_id,
            thread_id=args.thread_id,
            context_id=str(thread["context_id"]),
            event_kind="status_update",
            task_state="COMPLETED",
        ),
    )
    recovered = wait_projection(projections, binding_id, args.task_id, "completed")
    if str(recovered["slack_message_ts"]) != card_ts:
        raise AssertionError("Recovery replaced the Slack task card")
    replies = slack.raw.conversations_replies(channel=args.channel_id, ts=root_ts, limit=100)
    if not replies.get("ok"):
        raise RuntimeError("Could not inspect the Slack fixture thread")
    card_count = sum(1 for message in replies.get("messages", []) if message.get("ts") == card_ts)
    if card_count != 1 or len(replies.get("messages", [])) != 2:
        raise AssertionError("Recovery did not preserve one response card")
    print(
        json.dumps(
            {
                "binding_id": binding_id,
                "root_ts": root_ts,
                "task_id": args.task_id,
                "fallback": True,
                "recovered": True,
                "card_ts": card_ts,
                "response_count": card_count,
            }
        )
    )


if __name__ == "__main__":
    main()
