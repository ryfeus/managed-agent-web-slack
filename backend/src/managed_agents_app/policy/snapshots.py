"""Private immutable SQLite objects. DSQL alone selects the authoritative checkpoint."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from managed_agents_app.policy.models import PolicyError

MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


class SnapshotStore(Protocol):
    def get(self, key: str) -> bytes: ...
    def put(self, key: str, body: bytes) -> None: ...
    def delete(self, key: str) -> None: ...
    def list_old_keys(self, age_seconds: int) -> list[str]: ...


def validate_key(key: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}-[a-f0-9]{64}", key):
        raise PolicyError("policy-checkpoint-invalid")
    return key


def validate_snapshot(key: str, body: bytes) -> bytes:
    if len(body) > MAX_SNAPSHOT_BYTES or hashlib.sha256(body).hexdigest() != validate_key(key)[33:]:
        raise PolicyError("policy-checkpoint-invalid")
    return body


def snapshot(source: Path, destination: Path) -> bytes:
    # The helper has exited. Backup also captures committed WAL pages, unlike a file copy.
    with closing(sqlite3.connect(source)) as origin, closing(sqlite3.connect(destination)) as target:
        origin.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
        if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise PolicyError("policy-checkpoint-invalid")
    if destination.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise PolicyError("policy-checkpoint-too-large")
    return destination.read_bytes()


class FileSnapshotStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def get(self, key: str) -> bytes:
        path = self.directory / validate_key(key)
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise PolicyError("policy-checkpoint-too-large")
        return validate_snapshot(key, path.read_bytes())

    def put(self, key: str, body: bytes) -> None:
        validate_snapshot(key, body)
        path = self.directory / key
        try:
            with path.open("xb") as output:
                path.chmod(0o600)
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError:
            self.get(key)

    def delete(self, key: str) -> None:
        (self.directory / validate_key(key)).unlink(missing_ok=True)

    def list_old_keys(self, age_seconds: int) -> list[str]:
        cutoff = time.time() - age_seconds
        return [p.name for p in self.directory.iterdir() if p.is_file() and p.stat().st_mtime < cutoff]


class S3SnapshotStore:
    def __init__(self, bucket: str, client: Any | None = None) -> None:
        self.bucket = bucket
        self.client = client or boto3.client(
            "s3", config=Config(connect_timeout=2, read_timeout=5, retries={"total_max_attempts": 2})
        )

    def get(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=f"policy/{validate_key(key)}")
        stream = response["Body"]
        try:
            if response["ContentLength"] > MAX_SNAPSHOT_BYTES:
                raise PolicyError("policy-checkpoint-too-large")
            return validate_snapshot(key, stream.read(MAX_SNAPSHOT_BYTES + 1))
        finally:
            stream.close()

    def put(self, key: str, body: bytes) -> None:
        validate_snapshot(key, body)
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=f"policy/{key}",
                Body=body,
                IfNoneMatch="*",
                ServerSideEncryption="AES256",
                ContentType="application/vnd.sqlite3",
            )
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
            self.get(key)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=f"policy/{validate_key(key)}")

    def list_old_keys(self, age_seconds: int) -> list[str]:
        cutoff = datetime.now(UTC) - timedelta(seconds=age_seconds)
        return [
            str(item["Key"])[len("policy/") :]
            for page in self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix="policy/"
            )
            for item in page.get("Contents", [])
            if item["LastModified"] < cutoff
        ]
