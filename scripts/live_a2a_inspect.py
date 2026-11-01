#!/usr/bin/env python3
"""Read metadata for one deployed Slack binding during opt-in live acceptance."""

from __future__ import annotations

import argparse
import json

from managed_agents_app.config import AppConfig, load_config
from managed_agents_app.db.connection import connect
from managed_agents_app.managed_agent.client import ManagedAgentClient


def inspect_binding(
    config: AppConfig, team_id: str, channel_id: str, thread_ts: str, *, provider_evidence: bool = False
) -> dict:
    with connect(config, "admin") as conn:
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
            (team_id, f"{channel_id}:{thread_ts}"),
        ).fetchone()
        if binding is None:
            return {
                "binding": None,
                "tasks": [],
                "legacy_tables_present": [row["tablename"] for row in present],
            }
        surface_bindings = conn.execute(
            "SELECT binding_id, surface, tenant_id, external_thread_id "
            "FROM thread_surface_bindings WHERE thread_id = %s ORDER BY created_at",
            (binding["thread_id"],),
        ).fetchall()
        tasks = conn.execute(
            "SELECT a.task_id, a.client_message_id, p.status AS projection_status, "
            "p.last_task_state, c.a2a_state, "
            "EXISTS (SELECT 1 FROM cma_push_configs p "
            "WHERE p.task_id = a.task_id AND p.last_delivered_at IS NOT NULL) AS push_delivered "
            "FROM agent_tasks a "
            "LEFT JOIN slack_task_projections p ON p.binding_id = %s AND p.task_id = a.task_id "
            "LEFT JOIN cma_tasks c ON c.task_id = a.task_id "
            "WHERE a.thread_id = %s AND a.task_id IS NOT NULL "
            "ORDER BY a.created_at, a.task_binding_id",
            (binding["binding_id"], binding["thread_id"]),
        ).fetchall()
        input_requests = (
            conn.execute(
                "SELECT task_id, request_id, cma_event_id, status, decision, confirmation_attempted_at, "
                "human_ruling, review_digest "
                "FROM cma_input_requests "
                "WHERE task_id = ANY(%s) ORDER BY created_at",
                ([row["task_id"] for row in tasks],),
            ).fetchall()
            if tasks
            else []
        )
        clarification_requests = (
            conn.execute(
                "SELECT task_id, request_id, cma_event_id, status FROM cma_clarification_requests "
                "WHERE task_id = ANY(%s) ORDER BY created_at",
                ([row["task_id"] for row in tasks],),
            ).fetchall()
            if tasks
            else []
        )
        context = (
            conn.execute(
                "SELECT cma_session_id FROM cma_contexts WHERE context_id = %s",
                (binding["context_id"],),
            ).fetchone()
            if provider_evidence
            else None
        )
    evidence = []
    clarification_evidence = []
    interruption_evidence = {}
    if context and context["cma_session_id"]:
        events = ManagedAgentClient(config).list_events(str(context["cma_session_id"]))
        by_id = {str(e.get("id")): e for e in events}
        interrupts = [i for i, e in enumerate(events) if e.get("type") == "user.interrupt"]
        following = events[interrupts[-1] + 1 :] if interrupts else []
        interruption_evidence = {
            "interrupt_count": len(interrupts),
            "terminal_after_interrupt": any(
                str(e.get("type")).replace("thread_status", "status")
                in {"session.status_idle", "session.status_terminated"}
                and (e.get("stop_reason") or {}).get("type") != "requires_action"
                for e in following
            ),
        }
        for request in input_requests:
            event_id = str(request["cma_event_id"])
            source = by_id.get(event_id, {})
            evidence.append(
                {
                    "request_id": request["request_id"],
                    "tool_name": source.get("name"),
                    "confirmation_decisions": [
                        e.get("result")
                        for e in events
                        if e.get("type") == "user.tool_confirmation" and e.get("tool_use_id") == event_id
                    ],
                    "result_event_types": [
                        str(e.get("type"))
                        for e in events
                        if e.get("tool_use_id") == event_id and "result" in str(e.get("type"))
                    ],
                    "result_errors": [
                        bool(e.get("is_error"))
                        for e in events
                        if e.get("tool_use_id") == event_id and "result" in str(e.get("type"))
                    ],
                }
            )
        for request in clarification_requests:
            event_id = str(request["cma_event_id"])
            source = by_id.get(event_id, {})
            results = [
                e
                for e in events
                if e.get("type") == "user.custom_tool_result" and e.get("custom_tool_use_id") == event_id
            ]
            clarification_evidence.append(
                {
                    "request_id": request["request_id"],
                    "tool_name": source.get("name"),
                    "source_event_type": source.get("type"),
                    "result_count": len(results),
                    "result_errors": [bool(e.get("is_error")) for e in results],
                }
            )
    return {
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
                "push_delivered": row["push_delivered"],
                "clarification_requests": [
                    {"request_id": request["request_id"], "status": request["status"]}
                    for request in clarification_requests
                    if request["task_id"] == row["task_id"]
                ],
                "input_requests": [
                    {
                        "request_id": request["request_id"],
                        "status": request["status"],
                        "decision": request["decision"],
                        "confirmation_attempted_at": request["confirmation_attempted_at"],
                        "human_ruling": request["human_ruling"],
                        "review_digest": request["review_digest"],
                    }
                    for request in input_requests
                    if request["task_id"] == row["task_id"]
                ],
            }
            for row in tasks
        ],
        **(
            {
                "provider_evidence": evidence,
                "clarification_evidence": clarification_evidence,
                "interruption_evidence": interruption_evidence,
            }
            if provider_evidence
            else {}
        ),
        "surface_bindings": surface_bindings,
        "legacy_tables_present": [row["tablename"] for row in present],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-id", required=True)
    parser.add_argument("--channel-id", required=True)
    parser.add_argument("--thread-ts", required=True)
    parser.add_argument("--provider-evidence", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            inspect_binding(
                load_config(),
                args.team_id,
                args.channel_id,
                args.thread_ts,
                provider_evidence=args.provider_evidence,
            ),
            default=str,
        )
    )


if __name__ == "__main__":
    main()
