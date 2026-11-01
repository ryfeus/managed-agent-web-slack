---
name: "Blank agent"
description: "A blank starting point with the core toolset."
model: {"id": "claude-haiku-4-5-20251001", "speed": "standard"}
tools:
  - type: agent_toolset_20260401
    configs:
      - name: web_fetch
        type: web_fetch
        enabled: true
        permission_policy: {type: always_ask}
      - name: web_search
        type: web_search
        enabled: true
        permission_policy: {type: always_ask}
    default_config: {enabled: true, permission_policy: {type: always_allow}}
  - type: custom
    name: ask_user
    description: >-
      Ask the user a clarification question when essential information is missing.
      Set prompt to one clear question describing the information needed to continue.
      The application waits for the user's answer and returns it as the tool result.
      Use this for clarification only; tool permissions are decided automatically.
    input_schema:
      type: object
      properties:
        prompt:
          type: string
          description: The clarification question to display to the user.
      required: [prompt]
mcp_servers: []
skills: []
metadata: {}
---

You are a general-purpose agent that can research, write code, run commands, and use connected tools to complete the user's task end to end.

When essential information is missing, use ask_user to request clarification and wait for its result before continuing. Tool permissions are handled automatically; never use ask_user to request tool approval.
