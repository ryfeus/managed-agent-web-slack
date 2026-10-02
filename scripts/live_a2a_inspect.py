#!/usr/bin/env python3
"""Read metadata for one deployed Slack binding during opt-in live acceptance."""

from __future__ import annotations

import argparse
import json

from managed_agents_app.config import load_config
from managed_agents_app.db.connection import connect


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-id", required=True)
    parser.add_argument("--channel-id", required=True)
    parser.add_argument("--thread-ts", required=True)
    args = parser.parse_args()
    with connect(load_config(), "admin") as conn:
        legacy_tables = [
            "agent_sessions",
            "surface_bindings",
            "ingress_events",
            "projection_events",
            "agent_feedback",
            "slack_response_streams",
        ]
        present = conn.execute(
            "SELECT tablename FROM pg_catalog.pg_tables "
            "WHERE schemaname = 'public' AND tablename = ANY(%s) ORDER BY tablename",
            (legacy_tables,),
        ).fetchall()
        binding = conn.execute(
            "SELECT b.binding_id, t.thread_id, t.agent_id, t.context_id "
            "FROM thread_surface_bindings b JOIN agent_threads t ON t.thread_id = b.thread_id "
            "WHERE b.surface = 'slack' AND b.tenant_id = %s AND b.external_thread_id = %s",
            (args.team_id, f"{args.channel_id}:{args.thread_ts}"),
        ).fetchone()
        if binding is None:
            print(
                json.dumps(
                    {
                        "binding": None,
                        "tasks": [],
                        "legacy_tables_present": [row["tablename"] for row in present],
                    }
                )
            )
            return
        surface_bindings = conn.execute(
            "SELECT binding_id, surface, tenant_id, external_thread_id "
            "FROM thread_surface_bindings WHERE thread_id = %s ORDER BY created_at",
            (binding["thread_id"],),
        ).fetchall()
        tasks = conn.execute(
            "SELECT a.task_id, a.client_message_id, p.status AS projection_status, "
            "p.last_task_state, c.a2a_state, "
            "(SELECT COUNT(*) FROM a2a_task_event_receipts r "
            "WHERE r.task_id = a.task_id AND r.published_at IS NOT NULL) AS push_receipts "
            "FROM agent_tasks a "
            "LEFT JOIN slack_task_projections p ON p.binding_id = %s AND p.task_id = a.task_id "
            "LEFT JOIN cma_tasks c ON c.task_id = a.task_id "
            "WHERE a.thread_id = %s AND a.task_id IS NOT NULL "
            "ORDER BY a.created_at, a.task_binding_id",
            (binding["binding_id"], binding["thread_id"]),
        ).fetchall()
        input_requests = (
            conn.execute(
                "SELECT task_id, request_id, status, decision FROM cma_input_requests "
                "WHERE task_id = ANY(%s) ORDER BY created_at",
                ([row["task_id"] for row in tasks],),
            ).fetchall()
            if tasks
            else []
        )
    print(
        json.dumps(
            {
                "binding": {
                    "thread_id": str(binding["thread_id"]),
                    "agent_id": binding["agent_id"],
                    "context_id": binding["context_id"],
                },
                "tasks": [
                    {
                        "task_id": row["task_id"],
                        "client_message_id": row["client_message_id"],
                        "projection_status": row["projection_status"],
                        "task_state": row["last_task_state"],
                        "controller_state": row["a2a_state"],
                        "push_receipts": row["push_receipts"],
                        "input_requests": [
                            {
                                "request_id": request["request_id"],
                                "status": request["status"],
                                "decision": request["decision"],
                            }
                            for request in input_requests
                            if request["task_id"] == row["task_id"]
                        ],
                    }
                    for row in tasks
                ],
                "surface_bindings": surface_bindings,
                "legacy_tables_present": [row["tablename"] for row in present],
            },
            default=str,
        )
    )


if __name__ == "__main__":
    main()
