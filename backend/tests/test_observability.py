from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from managed_agents_app import observability
from managed_agents_app.cma_controller.provider import AnthropicCmaProvider


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


@pytest.mark.asyncio
async def test_provider_stream_cancellation_leaves_reader_blocking_canonical_work():
    # reproduce the actual adapter's live starvation mechanism without credentials.
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
    entered, release, finished, closed = Event(), Event(), Event(), Event()

    class Iterator:
        def __iter__(self):
            return self

        def __next__(self):
            entered.set()
            try:
                if not release.wait(2):
                    raise TimeoutError("fixture release deadline")
                raise StopIteration
            finally:
                finished.set()

        def close(self):
            closed.set()

    class Client:
        def stream_events(self, session_id, *, include_thinking):
            assert not include_thinking
            return Iterator()

        def list_events(self, session_id):
            return [{"type": "session.status_idle"}]

    provider = AnthropicCmaProvider(Client())
    stream = provider.stream_events("fixture")
    reader = asyncio.create_task(anext(stream))
    lookup = None
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert not finished.is_set()
        assert not closed.is_set()
        lookup = asyncio.create_task(provider.list_events("fixture"))
        await asyncio.sleep(0.025)
        assert not lookup.done()
        release.set()
        assert await asyncio.wait_for(lookup, 1) == [{"type": "session.status_idle"}]
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(reader, *([lookup] if lookup else []), return_exceptions=True)
        await stream.aclose()
