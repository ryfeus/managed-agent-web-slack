from __future__ import annotations

from threading import Barrier

from managed_agents_app.testing.local_queue import LocalQueue


def _success(_event: dict) -> dict:
    return {"batchItemFailures": []}


def test_queue_serializes_and_deletes_successful_messages() -> None:
    received: list[dict] = []
    queue = LocalQueue(lambda event: received.append(event) or _success(event))
    message_id = queue.send({"detail-type": "SlackMessageReceived", "detail": {"id": "one"}})

    result = queue.drain()

    assert received[0]["Records"][0]["messageId"] == message_id
    assert result["messages"] == [
        {"id": message_id, "receive_count": 1, "visible_at": 360, "state": "deleted"}
    ]


def test_queue_redelivers_only_after_visibility_expiry() -> None:
    attempts = 0

    def handler(event: dict) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {"batchItemFailures": [{"itemIdentifier": event["Records"][0]["messageId"]}]}
        return _success(event)

    queue = LocalQueue(handler)
    queue.send({"detail-type": "A", "detail": {}})
    queue.drain()
    assert queue.snapshot()["messages"][0]["state"] == "inflight"
    queue.drain()
    assert attempts == 1
    queue.advance_time(359)
    queue.drain()
    assert attempts == 1
    queue.advance_time(1)
    assert queue.drain()["messages"][0]["state"] == "deleted"
    assert attempts == 2


def test_queue_duplicate_delivery_uses_new_physical_message_ids() -> None:
    received: list[str] = []
    queue = LocalQueue(lambda event: received.append(event["Records"][0]["messageId"]) or _success(event))
    original = queue.send({"detail-type": "A", "detail": {"request": "same"}})
    duplicate = queue.duplicate(original)
    queue.drain()
    assert received == [original, duplicate]


def test_queue_moves_poison_messages_to_dlq_after_maximum_receives() -> None:
    def handler(event: dict) -> dict:
        return {"batchItemFailures": [{"itemIdentifier": event["Records"][0]["messageId"]}]}

    queue = LocalQueue(handler)
    queue.send({"detail-type": "A", "detail": {}})
    for _ in range(5):
        queue.drain()
        queue.advance_time(360)
    result = queue.drain()
    assert result["messages"][0]["state"] == "dlq"
    assert [item["status"] for item in result["history"]] == ["failed"] * 5 + ["dlq"]


def test_queue_honors_bounded_drains_and_concurrent_workers() -> None:
    bounded = LocalQueue(_success)
    bounded.send({"detail-type": "A", "detail": {}})
    bounded.send({"detail-type": "B", "detail": {}})
    assert len(bounded.drain(max_messages=1)["history"]) == 1

    barrier = Barrier(2)

    def handler(event: dict) -> dict:
        barrier.wait(timeout=2)
        return _success(event)

    queue = LocalQueue(handler)
    queue.send({"detail-type": "A", "detail": {}})
    queue.send({"detail-type": "B", "detail": {}})
    queue.drain(workers=2)
    assert [item["status"] for item in queue.snapshot()["history"]] == ["completed", "completed"]
