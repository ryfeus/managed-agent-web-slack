"""Optional metadata-only card-start hint; called after releasing the P0 claim."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

import boto3
from botocore.config import Config


@dataclass(frozen=True)
class SlackPreviewHint:
    binding_id: str
    task_id: str
    message_ts: str

    def wire(self) -> dict[str, Any]:
        return {
            "v": 1,
            "bindingId": self.binding_id,
            "taskId": self.task_id,
            "createdAt": int(time.time() * 1000),
        }


class SlackPreviewTrigger(Protocol):
    def start(self, hint: SlackPreviewHint) -> None: ...


class SqsSlackPreviewTrigger:
    def __init__(self, queue_url: str, client: Any) -> None:
        self.queue_url = queue_url
        self.client = client

    def start(self, hint: SlackPreviewHint) -> None:
        self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(hint.wire()),
            MessageGroupId=hint.binding_id,
            MessageDeduplicationId=hashlib.sha256(
                f"{hint.binding_id}\0{hint.task_id}\0{hint.message_ts}".encode()
            ).hexdigest(),
        )


def production_slack_preview_trigger(queue_url: str) -> SlackPreviewTrigger:
    return SqsSlackPreviewTrigger(
        queue_url,
        boto3.client(
            "sqs", config=Config(connect_timeout=0.1, read_timeout=0.15, retries={"total_max_attempts": 1})
        ),
    )
