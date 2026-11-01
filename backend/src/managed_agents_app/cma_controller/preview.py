"""Disposable P1 start hints. No preview I/O runs under the scheduler lease."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

import boto3
from botocore.config import Config


@dataclass(frozen=True)
class PreviewHint:
    task_id: str
    context_id: str
    session_id: str
    phase_id: str

    def wire(self) -> dict[str, Any]:
        return {
            "v": 1,
            "taskId": self.task_id,
            "contextId": self.context_id,
            "sessionId": self.session_id,
            "phaseId": self.phase_id,
            "createdAt": int(time.time() * 1000),
        }


class PreviewTrigger(Protocol):
    def start(self, hint: PreviewHint) -> None: ...


class SqsPreviewTrigger:
    def __init__(self, queue_url: str, client: Any) -> None:
        self.queue_url = queue_url
        self.client = client

    def start(self, hint: PreviewHint) -> None:
        self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(hint.wire()),
            MessageGroupId=hint.context_id,
            MessageDeduplicationId=hashlib.sha256(f"{hint.task_id}\0{hint.phase_id}".encode()).hexdigest(),
        )


def production_preview_trigger(queue_url: str) -> PreviewTrigger:
    return SqsPreviewTrigger(
        queue_url,
        boto3.client(
            "sqs", config=Config(connect_timeout=0.1, read_timeout=0.15, retries={"total_max_attempts": 1})
        ),
    )
