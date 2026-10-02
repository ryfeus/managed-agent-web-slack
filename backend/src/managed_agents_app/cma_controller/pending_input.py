"""Temporary handoff for turns CMA has not yet accepted."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Protocol


class PendingInputStore(Protocol):
    def put(self, key: str, text: str) -> None: ...
    def get(self, key: str) -> str: ...
    def delete(self, key: str) -> None: ...
    def list_keys(self) -> list[str]: ...
    def list_old_keys(self, age_seconds: int) -> list[str]: ...


class FilePendingInputStore:
    """Disk-backed local store; safe across controller process restarts."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not key or "/" in key or ".." in key:
            raise ValueError("Invalid pending-input key")
        return self.directory / key

    def put(self, key: str, text: str) -> None:
        path = self._path(key)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)

    def get(self, key: str) -> str:
        return self._path(key).read_text(encoding="utf-8")

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def list_keys(self) -> list[str]:
        return sorted(path.name for path in self.directory.iterdir() if path.is_file())

    def list_old_keys(self, age_seconds: int) -> list[str]:
        cutoff = time.time() - age_seconds
        return sorted(
            path.name for path in self.directory.iterdir() if path.is_file() and path.stat().st_mtime < cutoff
        )
