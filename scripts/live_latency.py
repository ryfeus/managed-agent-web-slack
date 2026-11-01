#!/usr/bin/env python3
"""API-only sandbox latency sampling; persist timing metadata, never provider bodies."""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from live import ROOT, cli, preflight, resources
from slack_user import SlackApi, marker_matches

from managed_agents_app.config import load_config
from managed_agents_app.db.connection import connect
from managed_agents_app.managed_agent.client import ManagedAgentClient

CASES = ("fresh", "reply", "web_fetch", "clarification")


class BenchmarkClient(httpx.Client):
    """Honor definitive Slack 429 responses without retrying ambiguous writes."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.rate_limit_retries = 0

    def post(self, url, **kwargs):
        for attempt in range(4):
            response = super().post(url, **kwargs)
            if response.status_code != 429:
                return response
            if attempt == 3:
                raise RuntimeError("RateLimitRetriesExhausted")
            wait = max(1, float(response.headers.get("retry-after", "5")))
            if wait > 60:
                raise RuntimeError("RateLimitDelayExceedsBudget")
            self.rate_limit_retries += 1
            while wait:
                chunk = min(wait, 30)
                time.sleep(chunk)
                wait -= chunk
        raise AssertionError("unreachable")


def epoch(value):
    if value is None:
        return None
    parsed = (
        value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None:
        raise ValueError("Timestamp requires a timezone")
    return parsed.timestamp()


def delta(start, end):
    a, b = epoch(start), epoch(end)
    return round(b - a, 4) if a is not None and b is not None else None


def describe(values):
    values = sorted(v for v in values if v is not None and math.isfinite(v))
    if not values:
        return {"n": 0}
    # Nearest-rank percentile: with six samples, p95 is the maximum, not a reliable tail estimate.
    return {
        "n": len(values),
        "min": min(values),
        "median": round(statistics.median(values), 4),
        "mean": round(statistics.mean(values), 4),
        "p95": values[math.ceil(0.95 * len(values)) - 1],
        "max": max(values),
    }


def summarize(samples):
    result = {}
    for case in CASES:
        rows = [r for r in samples if r["case"] == case]
        ok = [r for r in rows if r["outcome"] == "passed"]
        names = sorted({key for row in ok for key in row["seconds"]})
        result[case] = {
            "attempted": len(rows),
            "passed": len(ok),
            "failed": len(rows) - len(ok),
            "seconds": {name: describe([r["seconds"].get(name) for r in ok]) for name in names},
        }
    return result


def metadata(config, settings, root, source):
    with connect(config, "admin") as conn:
        return conn.execute(
            "SELECT t.thread_id, c.task_id, c.context_id, c.a2a_state, c.created_at, "
            "c.updated_at, c.dispatch_attempted_at, c.cma_input_event_id, c.cma_terminal_event_id, "
            "x.cma_session_id, p.status AS projection_status "
            "FROM thread_surface_bindings b JOIN agent_threads t ON t.thread_id = b.thread_id "
            "JOIN slack_task_projections p ON p.binding_id = b.binding_id "
            "JOIN cma_tasks c ON c.task_id = p.task_id JOIN cma_contexts x ON x.context_id = c.context_id "
            "WHERE b.surface = 'slack' AND b.tenant_id = %s AND b.external_thread_id = %s "
            "AND p.source_message_ts = %s",
            (settings["team_id"], f"{settings['channel']}:{root}", source),
        ).fetchone()


def trace(config, row, source):
    """Derive stage durations without copying event content or native arguments into evidence."""
    events = ManagedAgentClient(config).list_events(str(row["cma_session_id"]))
    begin = next(i for i, e in enumerate(events) if e["id"] == row["cma_input_event_id"])
    end = next(i for i, e in enumerate(events) if e["id"] == row["cma_terminal_event_id"])
    current = events[begin : end + 1]
    by_id = {str(e["id"]): e for e in current}
    input_at, terminal_at = current[0].get("processed_at"), current[-1].get("processed_at")
    replies = [e for e in current if e["type"] == "agent.message"]
    final = replies[-1] if replies else None
    calls = [e for e in current if e["type"] in ("agent.tool_use", "agent.mcp_tool_use")]
    custom = [e for e in current if e["type"] == "agent.custom_tool_use"]
    with connect(config, "admin") as conn:
        ingress = conn.execute(
            "SELECT created_at FROM surface_ingress_events WHERE task_id = %s", (row["task_id"],)
        ).fetchone()
        approvals = conn.execute(
            "SELECT cma_event_id, created_at, confirmation_attempted_at, status, decision "
            "FROM cma_input_requests WHERE task_id = %s ORDER BY created_at",
            (row["task_id"],),
        ).fetchall()
        projected = conn.execute(
            "SELECT projected_at FROM slack_projection_items WHERE task_id = %s "
            "AND item_kind = 'agent_message' AND item_id = %s AND status = 'posted'",
            (row["task_id"], f"cma-{final['id']}" if final else ""),
        ).fetchone()
    ingress_at = ingress["created_at"] if ingress else None
    if len(approvals) != len(calls):
        raise RuntimeError("MissingPolicyEvidence")
    final_at = final.get("processed_at") if final else None
    source_at = datetime.fromtimestamp(float(source), UTC)
    seconds = {
        "slack_to_ingress": delta(source_at, ingress_at),
        "ingress_to_task": delta(ingress_at, row["created_at"]),
        "task_to_provider_input": delta(row["created_at"], input_at),
        "provider_input_to_final": delta(input_at, final_at),
        "provider_input_to_terminal": delta(input_at, terminal_at),
        "terminal_to_controller_complete": delta(terminal_at, row["updated_at"]),
        "provider_final_to_projection": delta(final_at, projected["projected_at"] if projected else None),
        "slack_to_final_projection": delta(source_at, projected["projected_at"] if projected else None),
        "slack_to_controller_complete": delta(source_at, row["updated_at"]),
    }
    if approvals:
        approval = approvals[0]
        call = by_id[str(approval["cma_event_id"])]
        confirmation = next(
            (
                e
                for e in current
                if e["type"] == "user.tool_confirmation" and e.get("tool_use_id") == call["id"]
            ),
            {},
        )
        result = next(
            (e for e in current if e["type"] == "agent.tool_result" and e.get("tool_use_id") == call["id"]),
            {},
        )
        seconds.update(
            {
                "approval_discovery": delta(call.get("processed_at"), approval["created_at"]),
                "approval_discovery_to_confirmation_attempt": delta(
                    approval["created_at"], approval["confirmation_attempted_at"]
                ),
                "tool_call_to_confirmation": delta(
                    call.get("processed_at"), confirmation.get("processed_at")
                ),
                "provider_tool_execution": delta(
                    confirmation.get("processed_at"), result.get("processed_at")
                ),
            }
        )
        if not (
            len(approvals) == len(calls) == 1
            and approval["status"] == "resolved"
            and approval["decision"] == confirmation.get("result") == "allow"
            and approval["confirmation_attempted_at"] is not None
            and result
            and not result.get("is_error")
        ):
            raise RuntimeError("UnexpectedPolicyEvidence")
    if custom:
        question = custom[0]
        answer = next(
            (
                e
                for e in current
                if e["type"] == "user.custom_tool_result" and e.get("custom_tool_use_id") == question["id"]
            ),
            {},
        )
        seconds.update(
            {
                "provider_to_question": delta(input_at, question.get("processed_at")),
                "question_to_provider_answer": delta(
                    question.get("processed_at"), answer.get("processed_at")
                ),
                "provider_answer_to_final": delta(answer.get("processed_at"), final_at),
            }
        )
        if not (
            len(custom) == 1 and question.get("name") == "ask_user" and answer and not answer.get("is_error")
        ):
            raise RuntimeError("UnexpectedClarificationEvidence")
    return {
        "seconds": seconds,
        "tool_call_count": len(calls),
        "custom_call_count": len(custom),
        "negative_clock_deltas": [k for k, v in seconds.items() if v is not None and v < 0],
    }


def sample(settings, case, index, root=None):
    config = load_config()
    marker = f"LATENCY_{uuid4().hex}"
    prompt = {
        "fresh": f"Reply with exactly {marker}. Do not use tools or ask questions.",
        "reply": f"Reply with exactly {marker}. Do not use tools or ask questions.",
        "web_fetch": (
            "Use web_fetch exactly once on https://example.com. After it succeeds, "
            f"reply with exactly {marker}. Do not use any other tool or ask a question."
        ),
        "clarification": (
            'Use ask_user exactly once with prompt "Which environment should I use?". Wait for the answer. '
            f"Then reply with exactly {marker}. Do not use any other tool."
        ),
    }[case]
    result = {"case": case, "index": index, "outcome": "failed", "seconds": {}}
    started = time.monotonic()
    row = None
    stage = "slack_post"
    answered_at = None
    try:
        with BenchmarkClient(timeout=30) as client:
            slack = SlackApi(client)
            sent = slack.post(
                os.environ["SLACK_USER_TOKEN"],
                settings["channel"],
                prompt if root else f"<@{settings['bot_user_id']}> {prompt}",
                root,
            )
            root = root or sent["ts"]
            result.update({"thread_ts": root, "source_message_ts": sent["ts"]})
            result["seconds"]["post_api"] = round(time.monotonic() - started, 4)
            visible = None
            last_absent = result["seconds"]["post_api"]
            last_unsettled = last_absent
            question_at = None
            question_visible = None
            deadline = started + 240
            if case == "clarification":
                client.post(
                    settings["base_url"] + "/api/auth/session",
                    json={"accessToken": os.environ["WEB_ACCESS_TOKEN"]},
                ).raise_for_status()
            while time.monotonic() < deadline:
                stage = "slack_observation"
                messages = slack.thread(os.environ["SLACK_USER_TOKEN"], settings["channel"], root)["messages"]
                elapsed = time.monotonic() - started
                if (
                    any(
                        float(m["ts"]) >= float(sent["ts"]) and m.get("user") == settings["bot_user_id"]
                        for m in messages
                    )
                    and "first_bot_message" not in result["seconds"]
                ):
                    result["seconds"]["first_bot_message"] = round(elapsed, 4)
                if (
                    case == "clarification"
                    and question_visible is None
                    and any(
                        m.get("user") == settings["bot_user_id"]
                        and "Which environment should I use?" in m["content"]
                        for m in messages
                    )
                ):
                    question_visible = elapsed
                    result["seconds"]["question_visible"] = round(elapsed, 4)
                matches = marker_matches(
                    messages, marker, root, settings["user_id"], settings["bot_user_id"], None
                )
                if matches and visible is None:
                    visible = elapsed
                    result["seconds"].update(
                        {
                            "visible_final": round(visible, 4),
                            "visible_final_lower_bound": round(last_absent, 4),
                            "visible_observation_gap": round(visible - last_absent, 4),
                        }
                    )
                elif not matches:
                    last_absent = elapsed
                stage = "controller_metadata"
                row = metadata(config, settings, root, sent["ts"])
                if row:
                    result.update({"task_id": row["task_id"], "thread_id": str(row["thread_id"])})
                    if row["a2a_state"] in ("FAILED", "CANCELED", "REJECTED"):
                        raise RuntimeError("TerminalFailure")
                    if (
                        case == "clarification"
                        and row["a2a_state"] == "INPUT_REQUIRED"
                        and answered_at is None
                        and question_visible is not None
                    ):
                        stage = "clarification_answer"
                        question_at = time.monotonic()
                        history = client.get(
                            f"{settings['base_url']}/api/agui/threads/{row['thread_id']}/history"
                        )
                        history.raise_for_status()
                        interrupts = [
                            i
                            for m in history.json()["messages"]
                            for i in m.get("metadata", {})
                            .get("custom", {})
                            .get("agui", {})
                            .get("interrupts", [])
                        ]
                        if len(interrupts) != 1 or interrupts[0]["reason"] != "clarification":
                            raise RuntimeError("UnexpectedInterrupt")
                        answered_at = time.monotonic()
                        # The bridge streams through completion. Close after acknowledging response headers;
                        # disconnect does not cancel, and canonical metadata/Slack are polled independently.
                        with client.stream(
                            "POST",
                            settings["base_url"] + "/api/agui",
                            json={
                                "threadId": str(row["thread_id"]),
                                "runId": uuid4().hex,
                                "state": {},
                                "messages": [],
                                "tools": [],
                                "context": [],
                                "forwardedProps": {},
                                "resume": [
                                    {
                                        "interruptId": interrupts[0]["id"],
                                        "status": "resolved",
                                        "payload": {"answer": "staging"},
                                    }
                                ],
                            },
                        ) as response:
                            response.raise_for_status()
                            for line in response.iter_lines():
                                if '"RUN_ERROR"' in line:
                                    raise RuntimeError("ResumeRejected")
                                if '"RUN_STARTED"' in line or '"RUN_FINISHED"' in line:
                                    break
                        result["seconds"]["question_observed"] = round(question_at - started, 4)
                        result["seconds"]["answer_submit"] = round(answered_at - started, 4)
                    if (
                        visible is not None
                        and row["a2a_state"] == "COMPLETED"
                        and row["projection_status"] == "completed"
                    ):
                        settled = time.monotonic() - started
                        result["seconds"].update(
                            {
                                "settled_observed": round(settled, 4),
                                "settled_observed_lower_bound": round(last_unsettled, 4),
                                "settled_observation_gap": round(settled - last_unsettled, 4),
                            }
                        )
                        if answered_at is not None:
                            result["seconds"]["answer_to_visible_final"] = round(
                                started + visible - answered_at, 4
                            )
                        break
                last_unsettled = time.monotonic() - started
                time.sleep(2)
            else:
                raise TimeoutError("WorkflowDeadline")
        result["rate_limit_retries"] = client.rate_limit_retries
        stage = "provider_evidence"
        evidence = trace(config, row, sent["ts"])
        expected = (1 if case == "web_fetch" else 0, 1 if case == "clarification" else 0)
        if (evidence["tool_call_count"], evidence["custom_call_count"]) != expected:
            raise RuntimeError("UnexpectedToolCalls")
        result["seconds"].update(evidence.pop("seconds"))
        result.update(evidence)
        result["outcome"] = "passed"
    except Exception as error:
        result.update(
            {
                "failed_boundary": stage,
                "error_type": type(error).__name__,
                "elapsed_seconds": round(time.monotonic() - started, 4),
            }
        )
        # No exception text, arguments, transcript, response bodies, or credentials in reports.
        if row is None and result.get("source_message_ts"):
            with suppress(Exception):
                row = metadata(config, settings, root, result["source_message_ts"])
        if row and row["a2a_state"] in ("INPUT_REQUIRED", "WORKING", "SUBMITTED"):
            with httpx.Client(timeout=30) as cleanup:
                try:
                    cleanup.post(
                        settings["base_url"] + "/api/auth/session",
                        json={"accessToken": os.environ["WEB_ACCESS_TOKEN"]},
                    ).raise_for_status()
                    cleanup.post(
                        f"{settings['base_url']}/api/agui/threads/{row['thread_id']}/cancel"
                    ).raise_for_status()
                    result["cleanup"] = "stop_requested"
                except Exception:
                    result["cleanup"] = "unverified"
    print(json.dumps(result), flush=True)
    return result


def chain(settings, index):
    first = sample(settings, "fresh", index)
    rows = [first]
    if first["outcome"] == "passed":
        rows.append(sample(settings, "reply", index, first["thread_ts"]))
    else:
        rows.append(
            {
                "case": "reply",
                "index": index,
                "outcome": "not_run",
                "seconds": {},
                "failed_boundary": "parent_workflow",
            }
        )
    rows.append(sample(settings, "web_fetch", index))
    rows.append(sample(settings, "clarification", index))
    return rows


def cloudwatch(started, ended):
    state = cli(["terraform", "-chdir=infra/app", "show", "-json"])
    functions = [
        r["values"]["function_name"]
        for r in resources(state["values"]["root_module"])
        if r["type"] == "aws_lambda_function"
    ]
    result = {}
    for function in functions:
        metrics = {}
        for name, stats in (
            ("Duration", ["Average", "Maximum", "SampleCount", "Sum"]),
            ("Invocations", ["Sum"]),
            ("Errors", ["Sum"]),
            ("Throttles", ["Sum"]),
        ):
            data = cli(
                [
                    "aws",
                    "cloudwatch",
                    "get-metric-statistics",
                    "--namespace",
                    "AWS/Lambda",
                    "--metric-name",
                    name,
                    "--dimensions",
                    f"Name=FunctionName,Value={function}",
                    "--start-time",
                    started,
                    "--end-time",
                    ended,
                    "--period",
                    "60",
                    "--statistics",
                    *stats,
                ]
            )
            points = data.get("Datapoints", [])
            if name == "Duration":
                count = sum(p["SampleCount"] for p in points)
                metrics[name] = {
                    "unit": "milliseconds",
                    "sample_count": count,
                    "mean": sum(p["Sum"] for p in points) / count if count else None,
                    "max": max((p["Maximum"] for p in points), default=None),
                }
            else:
                metrics[name] = sum(p["Sum"] for p in points) if points else None
        result[function] = metrics
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=6, choices=range(1, 21))
    parser.add_argument("--workers", type=int, default=2, choices=(1, 2))
    parser.add_argument("--refresh-metrics", type=Path)
    args = parser.parse_args()
    settings = preflight(approval=True)
    with httpx.Client(timeout=30) as client:
        settings["bot_user_id"] = SlackApi(client).auth(os.environ["SLACK_BOT_TOKEN"])["user_id"]
    if args.refresh_metrics:
        report = json.loads(args.refresh_metrics.read_text())
        report["cloudwatch"] = cloudwatch(report["started_at"], report["ended_at"])
        args.refresh_metrics.write_text(json.dumps(report, indent=2))
        print(json.dumps({"report": str(args.refresh_metrics), "cloudwatch_refreshed": True}))
        return
    run_id = uuid4().hex
    output = ROOT / "test-results" / "latency" / run_id
    output.mkdir(parents=True)
    started = datetime.now(UTC).isoformat()
    samples = []
    print(
        json.dumps(
            {"run_id": run_id, "report": str(output / "report.json"), "planned_workflows": 4 * args.repeats}
        ),
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(chain, settings, index) for index in range(args.repeats)]
        for future in as_completed(futures):
            samples.extend(future.result())
            (output / "samples.json").write_text(json.dumps(samples, indent=2))
    ended = datetime.now(UTC).isoformat()
    report = {
        "run_id": run_id,
        "started_at": started,
        "ended_at": ended,
        "workers": args.workers,
        "poll_interval_seconds": 2,
        "scope": (
            "genuine Slack API delivery; clarification answered automatically via linked web API; "
            "no native Slack UI timing"
        ),
        "samples": samples,
        "summary": summarize(samples),
    }
    try:
        report["cloudwatch"] = cloudwatch(started, ended)
    except Exception as error:
        report["cloudwatch_unavailable"] = type(error).__name__
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps({"run_id": run_id, "report": str(output / "report.json"), "summary": report["summary"]}),
        flush=True,
    )
    if any(sample["outcome"] != "passed" for sample in samples):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
