"""Private SSE-S3 adapter for undelivered controller input."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import boto3


class S3PendingInputStore:
    def __init__(self, bucket: str, client: Any | None = None) -> None:
        if not bucket:
            raise ValueError("Pending-input bucket is required")
        self.bucket = bucket
        self.client = client or boto3.client("s3")

    def put(self, key: str, text: str) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=f"pending/{key}",
            Body=text.encode("utf-8"),
            ServerSideEncryption="AES256",
            ContentType="text/plain; charset=utf-8",
        )

    def get(self, key: str) -> str:
        result = self.client.get_object(Bucket=self.bucket, Key=f"pending/{key}")
        return str(result["Body"].read().decode("utf-8"))

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=f"pending/{key}")

    def list_keys(self) -> list[str]:
        keys: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix="pending/"):
            keys.extend(str(item["Key"])[len("pending/") :] for item in page.get("Contents", []))
        return keys

    def list_old_keys(self, age_seconds: int) -> list[str]:
        cutoff = datetime.now(UTC) - timedelta(seconds=age_seconds)
        keys: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix="pending/"):
            keys.extend(
                str(item["Key"])[len("pending/") :]
                for item in page.get("Contents", [])
                if item["LastModified"] < cutoff
            )
        return keys
