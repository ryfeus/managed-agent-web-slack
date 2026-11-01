"""Timing statistics and the benchmark's content-free evidence boundary."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "live_latency", Path(__file__).resolve().parents[2] / "scripts/live_latency.py"
)
assert SPEC and SPEC.loader
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_statistics_keep_sample_count_and_nearest_rank_tail():
    actual = benchmark.describe([None, math.nan, 1, 2, 3, 4, 5, 20])
    assert actual == {"n": 6, "min": 1, "median": 3.5, "mean": 5.8333, "p95": 20, "max": 20}
    assert benchmark.describe([]) == {"n": 0}
    summary = benchmark.summarize(
        [
            {"case": "fresh", "outcome": "passed", "seconds": {"visible_final": 2}},
            {"case": "fresh", "outcome": "failed", "seconds": {"visible_final": 100}},
        ]
    )
    assert summary["fresh"]["failed"] == 1
    assert summary["fresh"]["seconds"]["visible_final"]["median"] == 2


def test_timestamp_deltas_do_not_hide_skew_or_missing_data():
    assert benchmark.delta("2026-10-04T00:00:01Z", "2026-10-04T00:00:00Z") == -1
    assert benchmark.delta(None, "2026-10-04T00:00:00Z") is None
    with pytest.raises(ValueError, match="timezone"):
        benchmark.epoch("2026-10-04T00:00:00")


def test_trace_excludes_bodies_and_selects_only_current_turn(monkeypatch):
    at = datetime(2026, 10, 4, tzinfo=UTC)
    events = [
        {"id": "old", "type": "agent.tool_use", "processed_at": at, "input": {"secret": "must-not-leak"}},
        {
            "id": "input",
            "type": "user.message",
            "processed_at": at + timedelta(seconds=3),
            "content": "must-not-leak",
        },
        {
            "id": "final",
            "type": "agent.message",
            "processed_at": at + timedelta(seconds=8),
            "content": "must-not-leak",
        },
        {"id": "terminal", "type": "session.status_idle", "processed_at": at + timedelta(seconds=9)},
    ]
    monkeypatch.setattr(benchmark, "ManagedAgentClient", lambda _: Mock(list_events=lambda _: events))
    conn = Mock()
    conn.__enter__ = Mock(return_value=conn)
    conn.__exit__ = Mock(return_value=False)
    conn.execute.side_effect = [
        Mock(fetchone=lambda: {"created_at": at + timedelta(seconds=1)}),
        Mock(fetchall=lambda: []),
        Mock(fetchone=lambda: {"projected_at": at + timedelta(seconds=10)}),
    ]
    monkeypatch.setattr(benchmark, "connect", lambda *_: conn)
    row = {
        "cma_session_id": "private-provider-id",
        "task_id": "task",
        "cma_input_event_id": "input",
        "cma_terminal_event_id": "terminal",
        "created_at": at + timedelta(seconds=2),
        "updated_at": at + timedelta(seconds=9.5),
    }
    actual = benchmark.trace(None, row, str(at.timestamp()))
    assert actual["seconds"]["task_to_provider_input"] == 1
    assert actual["seconds"]["provider_input_to_final"] == 5
    assert actual["seconds"]["provider_final_to_projection"] == 2
    assert actual["seconds"]["slack_to_controller_complete"] == 9.5
    assert actual["tool_call_count"] == 0
    assert "must-not-leak" not in json.dumps(actual)
    assert "private-provider-id" not in json.dumps(actual)
    events.insert(2, {"id": "call", "type": "agent.tool_use", "processed_at": at + timedelta(seconds=4)})
    conn.execute.side_effect = [
        Mock(fetchone=lambda: {"created_at": at + timedelta(seconds=1)}),
        Mock(fetchall=lambda: []),
        Mock(fetchone=lambda: {"projected_at": at + timedelta(seconds=10)}),
    ]
    with pytest.raises(RuntimeError, match="MissingPolicyEvidence"):
        benchmark.trace(None, row, str(at.timestamp()))


def test_rate_limits_are_retried_but_ambiguous_writes_are_not(monkeypatch):
    waits = []
    monkeypatch.setattr(benchmark.time, "sleep", waits.append)
    calls = []

    def transport(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"retry-after": "2"})
            if len(calls) == 1
            else httpx.Response(200, json={"ok": True})
        )

    with benchmark.BenchmarkClient(transport=httpx.MockTransport(transport)) as client:
        assert client.post("https://slack.com/api/chat.postMessage").status_code == 200
    assert waits == [2]
    assert len(calls) == 2

    def ambiguous(request):
        calls.append(request)
        raise httpx.ReadTimeout("uncertain")

    calls.clear()
    with (
        benchmark.BenchmarkClient(transport=httpx.MockTransport(ambiguous)) as client,
        pytest.raises(httpx.ReadTimeout),
    ):
        client.post("https://slack.com/api/chat.postMessage")
    assert len(calls) == 1


def test_sample_observes_real_marker_helper_and_settled_task(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "fixture")
    monkeypatch.setattr(benchmark, "load_config", lambda: None)
    posted = {}

    class FakeSlack:
        def __init__(self, _):
            pass

        def post(self, token, channel, text, root):
            posted["marker"] = text.split("exactly ")[1].split(".")[0]
            return {"ts": "1.0"}

        def thread(self, token, channel, root):
            return {
                "messages": [{"ts": "2.0", "thread_ts": root, "user": "agent", "content": posted["marker"]}]
            }

    monkeypatch.setattr(benchmark, "SlackApi", FakeSlack)
    monkeypatch.setattr(
        benchmark,
        "metadata",
        lambda *_: {
            "task_id": "task",
            "thread_id": "thread",
            "a2a_state": "COMPLETED",
            "projection_status": "completed",
        },
    )
    monkeypatch.setattr(
        benchmark,
        "trace",
        lambda *_: {"seconds": {}, "tool_call_count": 0, "custom_call_count": 0},
    )
    actual = benchmark.sample({"channel": "channel", "user_id": "human", "bot_user_id": "agent"}, "fresh", 0)
    assert actual["outcome"] == "passed"
    assert actual["seconds"]["visible_final"] >= actual["seconds"]["post_api"]
