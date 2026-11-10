"""Trusted, explicitly registered application tools. No provider or surface access."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

MAX_TOOL_BYTES = 65536
TOOL_TIMEOUT_SECONDS = 10.0
TOOL_FAILURE = "The custom tool failed."
CONTROL_ERRORS = {
    "unknown-tool": "The custom tool is not available.",
    "invalid-arguments": "The custom tool arguments are invalid.",
    "denied": "Denied by OpenAPPA policy.",
    "cancelled": "Task cancelled by user.",
}


@dataclass(frozen=True)
class ToolContext:
    invocation_id: str
    deadline: float  # asyncio loop's monotonic clock


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool = False

    @classmethod
    def failure(cls) -> ToolResult:
        """A completed, declared failure; never carry private exception text."""
        return cls(TOOL_FAILURE, True)


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    input_model: type[BaseModel]
    handler: Callable[[BaseModel, ToolContext], Awaitable[ToolResult]]


class InvalidToolCall(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CustomToolRegistry:
    def __init__(self, tools: tuple[RegisteredTool, ...]) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        for tool in tools:
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name)
                or tool.name
                in {"ask_user", "bash", "read", "write", "edit", "glob", "grep", "web_fetch", "web_search"}
                or tool.name.startswith(("mcp__", "ant__"))
                or tool.name in self._tools
                or tool.input_model.model_config.get("extra") != "forbid"
            ):
                raise ValueError("Invalid custom tool registration")
            self._tools[tool.name] = tool

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def prepare(self, name: object, arguments: object) -> tuple[RegisteredTool, BaseModel]:
        if not isinstance(name, str) or name not in self._tools:
            raise InvalidToolCall("unknown-tool")
        tool = self._tools[name]
        try:
            if (
                not isinstance(arguments, dict)
                or len(json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                > MAX_TOOL_BYTES
            ):
                raise ValueError("Invalid arguments")
            validated = tool.input_model.model_validate(arguments, strict=True)
        except (ValueError, TypeError, UnicodeError, ValidationError) as error:
            raise InvalidToolCall("invalid-arguments") from error
        return tool, validated


class TextMetricsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str


async def text_metrics(arguments: BaseModel, _context: ToolContext) -> ToolResult:
    assert isinstance(arguments, TextMetricsInput)
    return ToolResult(
        json.dumps({"characters": len(arguments.text), "utf8_bytes": len(arguments.text.encode("utf-8"))})
    )


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PublishReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    audience: Literal["internal", "public"]
    text: str


DEMO_REPORT = "Synthetic quarterly report. Contact alex@example.com for details. Revenue: 42 demo credits."


async def demo_read_report(_arguments: BaseModel, _context: ToolContext) -> ToolResult:
    return ToolResult(DEMO_REPORT)


async def demo_prepare_release(_arguments: BaseModel, _context: ToolContext) -> ToolResult:
    return ToolResult("Synthetic release prepared. No external action performed.")


async def demo_publish_report(arguments: BaseModel, context: ToolContext) -> ToolResult:
    assert isinstance(arguments, PublishReportInput)
    return ToolResult(
        json.dumps({"simulated": True, "audience": arguments.audience, "receipt": context.invocation_id})
    )


def default_custom_tools() -> CustomToolRegistry:
    return CustomToolRegistry(
        (
            RegisteredTool("text_metrics", TextMetricsInput, text_metrics),
            RegisteredTool("demo_read_private_report", EmptyInput, demo_read_report),
            RegisteredTool("demo_read_contact_report", EmptyInput, demo_read_report),
            RegisteredTool("demo_prepare_release", EmptyInput, demo_prepare_release),
            RegisteredTool("demo_publish_report", PublishReportInput, demo_publish_report),
        )
    )
