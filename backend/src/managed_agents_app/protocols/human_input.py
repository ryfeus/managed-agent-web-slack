"""Provider-neutral human-input/v1 extension payloads."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from pydantic.alias_generators import to_camel


def _approval_responses() -> list[Literal["allow", "deny"]]:
    return ["allow", "deny"]


class ExtensionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)


class ToolDescriptor(ExtensionPayload):
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]


class ToolApprovalRequest(ExtensionPayload):
    kind: Literal["tool_approval"] = "tool_approval"
    request_id: str = Field(min_length=1)
    tool: ToolDescriptor
    allowed_responses: list[Literal["allow", "deny"]] = Field(default_factory=_approval_responses)
    message: str | None = None

    @field_validator("allowed_responses")
    @classmethod
    def both_decisions_required(cls, value: list[str]) -> list[str]:
        if value != ["allow", "deny"]:
            raise ValueError("allowedResponses must be ['allow', 'deny']")
        return value


class ClarificationRequest(ExtensionPayload):
    kind: Literal["clarification"] = "clarification"
    request_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    choices: list[str] | None = None


HumanInputRequest = Annotated[ToolApprovalRequest | ClarificationRequest, Field(discriminator="kind")]


class ToolApprovalResponse(ExtensionPayload):
    kind: Literal["tool_approval"] = "tool_approval"
    request_id: str = Field(min_length=1)
    decision: Literal["allow", "deny"]
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def reason_only_for_denial(self) -> ToolApprovalResponse:
        if self.reason is not None and self.decision != "deny":
            raise ValueError("A reason is allowed only for denial")
        return self


class ClarificationResponse(ExtensionPayload):
    kind: Literal["clarification"] = "clarification"
    request_id: str = Field(min_length=1)
    answer: str = Field(min_length=1, max_length=20_000)


HumanInputResponse = Annotated[ToolApprovalResponse | ClarificationResponse, Field(discriminator="kind")]
