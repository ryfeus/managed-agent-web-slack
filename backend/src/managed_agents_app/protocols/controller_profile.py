"""Application controller profile above base A2A.

Base A2A message/send supports both blocking and returnImmediately=true sends.
An application preference for async sends must be negotiated by its own profile.
"""

from dataclasses import dataclass
from typing import Literal

ASYNC_COPILOT_PROFILE_URI = (
    "https://ryfeus.github.io/claude-managed-agents-ui-eda/a2a/extensions/async-copilot-controller/v1"
)
HUMAN_INPUT_EXTENSION_URI = (
    "https://ryfeus.github.io/claude-managed-agents-ui-eda/a2a/extensions/human-input/v1"
)
# Application profile metadata. This is not a base A2A authentication header.
A2A_DELIVERY_ID_HEADER = "X-A2A-Delivery-Id"


@dataclass(frozen=True)
class ControllerProfile:
    uri: str = ASYNC_COPILOT_PROFILE_URI
    transport: Literal["http-json"] = "http-json"
    streaming: bool = True
    durable_task_history: bool = True
    cancel_task: bool = True
    push_notifications: bool = True
    durable_message_id_idempotency: bool = True
    conversational_output: Literal["message"] = "message"
    produced_object_output: Literal["artifact"] = "artifact"


@dataclass(frozen=True)
class ControllerBehavior:
    """Capabilities negotiated separately from the required profile."""

    human_input: bool = False
    concurrent_tasks: bool = False
    queue_while_input_required: bool = False
    produces_artifacts: bool = False


CMA_INITIAL_BEHAVIOR = ControllerBehavior(human_input=True)
