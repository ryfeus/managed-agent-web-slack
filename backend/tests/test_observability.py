from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from managed_agents_app import observability


@pytest.mark.asyncio
async def test_timings_preserve_results_errors_and_task_scope_without_content(monkeypatch):
    records = []
    monkeypatch.setattr(observability._logger, "info", lambda value: records.append(json.loads(value)))
    with observability.task_scope("application-task"):
        assert (
            await observability.blocking("fixture_read", lambda argument: argument, "private-body")
            == "private-body"
        )
        with pytest.raises(ValueError, match="private-error-body"):
            await observability.blocking(
                "fixture_failure", lambda: (_ for _ in ()).throw(ValueError("private-error-body"))
            )
    observability.emit("outside_scope")
    assert all(r["task_id"] == "application-task" for r in records[:-1])
    assert records[-1]["task_id"] is None
    assert "private-body" not in json.dumps(records)
    assert "private-error-body" not in json.dumps(records)
    failure = next(r for r in records if r.get("outcome") == "error")
    assert failure["error_type"] == "ValueError"


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_hide_running_worker_or_executor_queue_wait(monkeypatch):
    records = []
    monkeypatch.setattr(observability._logger, "info", lambda value: records.append(json.loads(value)))
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
    started, release, finished = Event(), Event(), Event()

    def stream_read():
        started.set()
        try:
            release.wait(2)
        finally:
            finished.set()

    read = asyncio.create_task(observability.blocking("fixture_stream_read", stream_read))
    try:
        async with asyncio.timeout(1):
            while not started.is_set():
                await asyncio.sleep(0.001)
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read
        lookup = asyncio.create_task(observability.blocking("fixture_task_read", lambda: "completed"))
        await asyncio.sleep(0.025)
        assert not lookup.done()
        assert not finished.is_set()
        release.set()
        assert await asyncio.wait_for(lookup, 1) == "completed"
        started_lookup = next(
            r for r in records if r["event"] == "blocking_started" and r["operation"] == "fixture_task_read"
        )
        assert started_lookup["queue_ms"] >= 20
        assert started_lookup["executor_workers"] == 1
        cancelled = next(r for r in records if r["event"] == "blocking_waiter_cancelled")
        worker_end = next(
            r for r in records if r["event"] == "blocking_finished" and r["span_id"] == cancelled["span_id"]
        )
        assert worker_end["outcome"] == "success"
    finally:
        release.set()
        await asyncio.gather(read, return_exceptions=True)
