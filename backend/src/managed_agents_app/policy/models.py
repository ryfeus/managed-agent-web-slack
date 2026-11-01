"""Runtime-independent policy requests and decisions; no provider imports."""

from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class ToolPolicyRequest:
    root_id: str
    invocation_id: str
    agent_id: str
    tool_kind: Literal["builtin", "mcp"]
    tool_name: str
    arguments: dict[str, Any]
    runtime: str = "cma"


@dataclass(frozen=True)
class HumanReview:
    authority: str
    fingerprint: str
    description: str


@dataclass(frozen=True)
class ToolPolicyDecision:
    decision: Literal["allow", "deny", "human_review"]
    reason: str | None = None
    review: HumanReview | None = None


class PolicyError(RuntimeError):
    """Operational failure, never an authorization decision."""

    def __init__(self, code: str = "policy-unavailable") -> None:
        super().__init__(code)
        self.code = code


class ToolPolicyEngine(Protocol):
    async def evaluate_tool_call(self, request: ToolPolicyRequest, timeout: float) -> ToolPolicyDecision: ...

    async def resolve_tool_call(
        self,
        request: ToolPolicyRequest,
        review: HumanReview,
        ruling: Literal["allow", "deny"],
        timeout: float,
    ) -> ToolPolicyDecision: ...
