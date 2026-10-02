"""A2A v1 push configuration policy for the trusted control plane."""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import suppress
from urllib.parse import urlsplit, urlunsplit

from a2a.types import a2a_pb2 as a2a
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError

from managed_agents_app.cma_controller.push_repository import PushConfigConflict, PushRepository
from managed_agents_app.cma_controller.push_trigger import PushTrigger

_CONFIG_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def normalized_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
        or not parsed.path.startswith("/")
    ):
        raise InvalidParamsError("Invalid push callback URL")
    try:
        port = parsed.port
    except ValueError as error:
        raise InvalidParamsError("Invalid push callback URL") from error
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host + (f":{port}" if port and port != {"http": 80, "https": 443}[parsed.scheme] else "")
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


class PushConfigService:
    def __init__(self, repository: PushRepository, allowed_url: str, trigger: PushTrigger) -> None:
        self.repository = repository
        self.allowed_url = normalized_url(allowed_url) if allowed_url else ""
        if repository.runtime.config.app_env == "production" and self.allowed_url.startswith("http://"):
            raise ValueError("Production push callback must use HTTPS")
        self.trigger = trigger

    @staticmethod
    def _wire(row: dict[str, object]) -> a2a.TaskPushNotificationConfig:
        return a2a.TaskPushNotificationConfig(
            id=str(row["config_id"]), task_id=str(row["task_id"]), url=str(row["url"])
        )

    async def create(
        self, config: a2a.TaskPushNotificationConfig, task_id: str = ""
    ) -> a2a.TaskPushNotificationConfig:
        resolved_task_id = task_id or config.task_id
        if not resolved_task_id or (config.task_id and config.task_id != resolved_task_id):
            raise InvalidParamsError("Push taskId does not match the Task")
        if config.token or config.HasField("authentication"):
            raise InvalidParamsError("Push token and authentication are unsupported")
        if not self.allowed_url or normalized_url(config.url) != self.allowed_url:
            raise InvalidParamsError("Push callback URL is not allowed")
        config_id = config.id or f"inline-{hashlib.sha256(self.allowed_url.encode()).hexdigest()[:24]}"
        if not _CONFIG_ID.fullmatch(config_id) or config_id in {".", ".."}:
            raise InvalidParamsError("Invalid push configuration ID")
        try:
            row = await asyncio.to_thread(
                self.repository.create, resolved_task_id, config_id, self.allowed_url
            )
        except KeyError as error:
            raise TaskNotFoundError() from error
        except PushConfigConflict as error:
            raise InvalidParamsError(str(error)) from error
        task = await asyncio.to_thread(self.repository.runtime.get_task, resolved_task_id)
        assert task is not None
        # Maintenance reconstructs current state if enqueue is lost.
        with suppress(Exception):
            self.trigger.schedule_context(str(task["context_id"]), "push-config-created")
        return self._wire(row)

    async def get(self, task_id: str, config_id: str) -> a2a.TaskPushNotificationConfig:
        row = await asyncio.to_thread(self.repository.get, task_id, config_id)
        if row is None:
            raise TaskNotFoundError()
        return self._wire(row)

    async def list(
        self, task_id: str, page_size: int, page_token: str
    ) -> a2a.ListTaskPushNotificationConfigsResponse:
        if await asyncio.to_thread(self.repository.runtime.get_task, task_id) is None:
            raise TaskNotFoundError()
        size = page_size or 50
        if not 1 <= size <= 100:
            raise InvalidParamsError("Invalid push config page size")
        rows = await asyncio.to_thread(self.repository.list_configs, task_id, page_token, size + 1)
        selected = rows[:size]
        return a2a.ListTaskPushNotificationConfigsResponse(
            configs=[self._wire(row) for row in selected],
            next_page_token=str(selected[-1]["config_id"]) if len(rows) > size else "",
        )

    async def delete(self, task_id: str, config_id: str) -> None:
        if await asyncio.to_thread(self.repository.runtime.get_task, task_id) is None:
            raise TaskNotFoundError()
        await asyncio.to_thread(self.repository.delete, task_id, config_id)
        # A successful delete must outlive a callback already in flight.
        for _ in range(160):
            if await asyncio.to_thread(self.repository.deletion_settled, task_id, config_id):
                await asyncio.to_thread(self.repository.purge_deleted, task_id, config_id)
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("Push delivery did not settle; retry deletion")
