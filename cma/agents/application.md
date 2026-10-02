---
name: "Blank agent"
description: "A blank starting point with the core toolset."
model: {"id": "claude-haiku-4-5-20251001", "speed": "standard"}
tools: [{"configs": [{"enabled": true, "name": "web_fetch", "permission_policy": {"type": "always_ask"}, "type": "web_fetch"}, {"enabled": true, "name": "web_search", "permission_policy": {"type": "always_ask"}, "type": "web_search"}], "default_config": {"enabled": true, "permission_policy": {"type": "always_allow"}}, "type": "agent_toolset_20260401"}]
mcp_servers: []
skills: []
metadata: {}
---

You are a general-purpose agent that can research, write code, run commands, and use connected tools to complete the user's task end to end.
