"""Runtime-independent policy requests and decisions; no provider imports."""

from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class PolicyState:
    key: str | None = None
    revision: int = 0
    policy_digest: str | None = None

    @classmethod
    def from_context(cls, context: dict[str, Any]) -> PolicyState:
        return cls(
            context.get("policy_checkpoint_key"),
            int(context.get("policy_checkpoint_revision") or 0),
            context.get("policy_digest"),
        )


@dataclass(frozen=True)
class PolicyCheckpoint:
    expected_revision: int
    key: str
    policy_digest: str


@dataclass(frozen=True)
class ToolPolicyRequest:
    root_id: str
    invocation_id: str
    agent_id: str
    tool_kind: Literal["builtin", "mcp", "custom"]
    tool_name: str
    arguments: dict[str, Any]
    runtime: str = "cma"
    state: PolicyState | None = None


@dataclass(frozen=True)
class HumanReview:
    authority: str
    fingerprint: str
    description: str


@dataclass(frozen=True)
class ToolAuthorization:
    """Private, temporary evidence for reconstructing one authorized native dispatch."""

    policy: str
    fingerprint: str
    review: HumanReview | None = None
    ruling: Literal["allow", "deny"] | None = None


@dataclass(frozen=True)
class ToolPolicyDecision:
    decision: Literal["allow", "deny", "human_review"]
    reason: str | None = None
    review: HumanReview | None = None
    authorization: ToolAuthorization | None = None
    checkpoint: PolicyCheckpoint | None = None


@dataclass(frozen=True)
class ToolResultDecision:
    decision: Literal["deliver", "withhold"]
    checkpoint: PolicyCheckpoint | None = None
    text: str | None = None  # Exact native admitted bytes, never a raw-output fallback.


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

    async def evaluate_tool_result(
        self,
        request: ToolPolicyRequest,
        authorization: ToolAuthorization,
        text: str,
        is_error: bool,
        timeout: float,
    ) -> ToolResultDecision: ...
