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
    name: text_metrics
    description: >-
      Count the Unicode code points and UTF-8 bytes in the exact supplied text.
      Use this for deterministic text sizing; characters are code points, not visual grapheme clusters.
      Returns JSON with characters and utf8_bytes. This tool does not store text or call external services.
    input_schema:
      type: object
      properties:
        text:
          type: string
      required: [text]
      additionalProperties: false
  - type: custom
    name: demo_read_private_report
    description: Read a fixed synthetic report with an example email. Requires human review and restricts subsequent demo publication to internal audiences.
    input_schema: {type: object, properties: {}, additionalProperties: false}
  - type: custom
    name: demo_read_contact_report
    description: Read the same synthetic report through native email redaction. Use in a new conversation before any private read; returns admitted redacted text.
    input_schema: {type: object, properties: {}, additionalProperties: false}
  - type: custom
    name: demo_prepare_release
    description: Prepare a synthetic release. Records the prerequisite for demo publication. Performs no external action.
    input_schema: {type: object, properties: {}, additionalProperties: false}
  - type: custom
    name: demo_publish_report
    description: Simulate publication of the supplied text. Requires successful preparation, compatible audience and human review. Repeated publication requires an explicit native effect-history exception. Never publishes externally.
    input_schema:
      type: object
      properties:
        audience: {type: string, enum: [internal, public]}
        text: {type: string}
      required: [audience, text]
      additionalProperties: false
  - type: custom
    name: ask_user
    description: >-
      Ask the user a clarification question when essential information is missing.
      Set prompt to one clear question describing the information needed to continue.
      The application waits for the user's answer and returns it as the tool result.
      Use this for clarification only; tool authorization is handled separately by the application.
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

When essential information is missing, use ask_user to request clarification and wait for its result before continuing. Tool permissions are handled by the application; never use ask_user to request tool approval.

The demo_* tools are synthetic policy demonstrations. Follow requested steps in order and wait for each result before the next call. Use demo_prepare_release before demo_publish_report only when the user requests preparation; a deliberate attempt without preparation demonstrates denial. Pass only admitted report text to publication. If policy denies an operation, explain the denial without switching tools to bypass it. Human review is provided by the application; never use ask_user for approval. A publication receipt is simulated and does not mean anything was sent externally.
