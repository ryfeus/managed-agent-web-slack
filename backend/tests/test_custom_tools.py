from __future__ import annotations

import json

import pytest

from managed_agents_app.cma_controller.custom_tools import (
    MAX_TOOL_BYTES,
    CustomToolRegistry,
    InvalidToolCall,
    RegisteredTool,
    TextMetricsInput,
    ToolContext,
    default_custom_tools,
    text_metrics,
)


@pytest.mark.asyncio
async def test_text_metrics_counts_codepoints_and_utf8_without_normalizing():
    registry = default_custom_tools()
    tool, args = registry.prepare("text_metrics", {"text": "é👩‍💻\n"})
    result = await tool.handler(args, ToolContext("invocation", 10))
    assert json.loads(result.text) == {"characters": 5, "utf8_bytes": 14}
    assert not result.is_error


@pytest.mark.parametrize("arguments", [None, [], {"text": 12}, {"text": "x", "extra": True}, {}])
def test_argument_validation_is_strict(arguments):
    with pytest.raises(InvalidToolCall, match="invalid-arguments"):
        default_custom_tools().prepare("text_metrics", arguments)


def test_size_limit_uses_utf8_and_does_not_count_characters_as_bytes():
    with pytest.raises(InvalidToolCall, match="invalid-arguments"):
        default_custom_tools().prepare("text_metrics", {"text": "😀" * (MAX_TOOL_BYTES // 4)})
    with pytest.raises(InvalidToolCall, match="unknown-tool"):
        default_custom_tools().prepare("unregistered", {})


@pytest.mark.parametrize("name", ["ask_user", "web_fetch", "bash", "ant__control", "mcp__tool", "a/b", ""])
def test_reserved_or_ambiguous_names_are_not_registered(name):
    with pytest.raises(ValueError, match="registration"):
        CustomToolRegistry((RegisteredTool(name, TextMetricsInput, text_metrics),))
