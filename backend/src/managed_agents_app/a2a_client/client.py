"""The sole SDK construction point for application control-plane traffic."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import httpx
from a2a.client.card_resolver import A2ACardResolver
from a2a.client.client import Client, ClientCallContext, ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.client.errors import A2AClientError
from a2a.extensions.common import HTTP_EXTENSION_HEADER
from a2a.types import a2a_pb2 as a2a
from a2a.utils.constants import TransportProtocol
from a2a.utils.errors import InvalidParamsError, InvalidRequestError, MethodNotFoundError

from managed_agents_app.agents.registry import AgentRegistry
from managed_agents_app.protocols.controller_profile import HUMAN_INPUT_EXTENSION_URI


class ControllerProfileError(ValueError):
    pass


class A2AControlPlaneError(RuntimeError):
    pass


class DefinitiveA2ARequestError(A2AControlPlaneError):
    """The controller explicitly rejected the request before Task admission."""


class AmbiguousA2ATransportError(A2AControlPlaneError):
    """The request may have reached the controller; retry with its message ID."""


def _send_error(error: Exception) -> A2AControlPlaneError:
    if isinstance(error, (InvalidParamsError, InvalidRequestError, MethodNotFoundError)):
        return DefinitiveA2ARequestError(str(error))
    if (
        isinstance(error, A2AClientError)
        and isinstance(error.__cause__, httpx.HTTPStatusError)
        and error.__cause__.response.status_code in {401, 403, 404, 405}
    ):
        return DefinitiveA2ARequestError(str(error))
    return AmbiguousA2ATransportError(str(error))


class A2AControllerClient:
    def __init__(
        self, registry: AgentRegistry, http: httpx.AsyncClient | None = None, card_ttl_seconds: int = 300
    ) -> None:
        self.registry = registry
        self.http = http or httpx.AsyncClient(timeout=15)
        self._owns_http = http is None
        self.card_ttl_seconds = card_ttl_seconds
        self._cache: dict[str, tuple[float, a2a.AgentCard, Client]] = {}

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def _entry(self, agent_id: str) -> tuple[a2a.AgentCard, Client]:
        registration = self.registry.get(agent_id)
        if registration is None:
            raise KeyError(f"Unknown agent: {agent_id}")
        cached = self._cache.get(agent_id)
        if cached and cached[0] > time.monotonic():
            return cached[1], cached[2]
        card = await A2ACardResolver(self.http, registration.endpoint).get_agent_card()
        if not any(
            interface.protocol_binding == TransportProtocol.HTTP_JSON.value
            and interface.url.rstrip("/") == registration.endpoint.rstrip("/")
            for interface in card.supported_interfaces
        ):
            raise ControllerProfileError("Controller has no matching HTTP+JSON interface")
        if not card.capabilities.streaming or not card.capabilities.push_notifications:
            raise ControllerProfileError("Controller must support streaming and push")
        if registration.required_profile not in {extension.uri for extension in card.capabilities.extensions}:
            raise ControllerProfileError("Controller does not advertise its required profile")
        client = ClientFactory(
            ClientConfig(
                httpx_client=self.http,
                supported_protocol_bindings=[TransportProtocol.HTTP_JSON.value],
                streaming=False,
            )
        ).create(card)
        self._cache[agent_id] = (time.monotonic() + self.card_ttl_seconds, card, client)
        return card, client

    async def get_agent_card(self, agent_id: str) -> a2a.AgentCard:
        return (await self._entry(agent_id))[0]

    async def validate_profile(self, agent_id: str) -> bool:
        card = await self.get_agent_card(agent_id)
        return HUMAN_INPUT_EXTENSION_URI in {extension.uri for extension in card.capabilities.extensions}

    async def send_message(
        self, agent_id: str, message: a2a.Message, *, human_input: bool = False
    ) -> a2a.Task:
        _, client = await self._entry(agent_id)
        context = None
        if human_input:
            if not await self.validate_profile(agent_id):
                raise ControllerProfileError("Controller does not support human-input/v1")
            context = ClientCallContext(service_parameters={HTTP_EXTENSION_HEADER: HUMAN_INPUT_EXTENSION_URI})
        request = a2a.SendMessageRequest(
            message=message, configuration=a2a.SendMessageConfiguration(return_immediately=True)
        )
        try:
            async for response in client.send_message(request, context=context):
                if response.WhichOneof("payload") == "task":
                    return response.task
        except Exception as error:
            raise _send_error(error) from error
        raise AmbiguousA2ATransportError("Controller did not return a Task")

    async def get_task(
        self, agent_id: str, task_id: str, *, human_input: bool = False, history_length: int | None = None
    ) -> a2a.Task:
        _, client = await self._entry(agent_id)
        request = a2a.GetTaskRequest(id=task_id)
        if history_length is not None:
            request.history_length = history_length
        context = (
            ClientCallContext(service_parameters={HTTP_EXTENSION_HEADER: HUMAN_INPUT_EXTENSION_URI})
            if human_input
            else None
        )
        return await client.get_task(request, context=context)

    async def list_tasks(
        self, agent_id: str, context_id: str, page_size: int = 50, page_token: str = ""
    ) -> a2a.ListTasksResponse:
        _, client = await self._entry(agent_id)
        return await client.list_tasks(
            a2a.ListTasksRequest(context_id=context_id, page_size=page_size, page_token=page_token)
        )

    async def subscribe_task(
        self, agent_id: str, task_id: str, *, human_input: bool = False
    ) -> AsyncIterator[a2a.StreamResponse]:
        card, _ = await self._entry(agent_id)
        client = ClientFactory(
            ClientConfig(
                httpx_client=self.http,
                supported_protocol_bindings=[TransportProtocol.HTTP_JSON.value],
                streaming=True,
            )
        ).create(card)
        context = (
            ClientCallContext(service_parameters={HTTP_EXTENSION_HEADER: HUMAN_INPUT_EXTENSION_URI})
            if human_input
            else None
        )
        async for response in client.subscribe(a2a.SubscribeToTaskRequest(id=task_id), context=context):
            yield response

    async def cancel_task(self, agent_id: str, task_id: str) -> a2a.Task:
        _, client = await self._entry(agent_id)
        return await client.cancel_task(a2a.CancelTaskRequest(id=task_id))

    async def create_push_config(
        self, agent_id: str, task_id: str, config_id: str, url: str
    ) -> a2a.TaskPushNotificationConfig:
        _, client = await self._entry(agent_id)
        return await client.create_task_push_notification_config(
            a2a.TaskPushNotificationConfig(task_id=task_id, id=config_id, url=url)
        )

    async def get_push_config(
        self, agent_id: str, task_id: str, config_id: str
    ) -> a2a.TaskPushNotificationConfig:
        _, client = await self._entry(agent_id)
        return await client.get_task_push_notification_config(
            a2a.GetTaskPushNotificationConfigRequest(task_id=task_id, id=config_id)
        )

    async def list_push_configs(
        self, agent_id: str, task_id: str, page_size: int = 50, page_token: str = ""
    ) -> a2a.ListTaskPushNotificationConfigsResponse:
        _, client = await self._entry(agent_id)
        return await client.list_task_push_notification_configs(
            a2a.ListTaskPushNotificationConfigsRequest(
                task_id=task_id, page_size=page_size, page_token=page_token
            )
        )

    async def delete_push_config(self, agent_id: str, task_id: str, config_id: str) -> None:
        _, client = await self._entry(agent_id)
        await client.delete_task_push_notification_config(
            a2a.DeleteTaskPushNotificationConfigRequest(task_id=task_id, id=config_id)
        )
