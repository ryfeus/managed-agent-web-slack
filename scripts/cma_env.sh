#!/usr/bin/env bash
# Source this after loading .env in local controller/live-test launchers.
# The caller supplies CMA_REPOSITORY_ROOT; the runtime never parses the lock.
cma_resolved_json="$(python3 "$CMA_REPOSITORY_ROOT/scripts/cma_lock.py" json)" || return 1
export CLAUDE_AGENT_ID="$(jq -r '.agent_id' <<<"$cma_resolved_json")"
export CLAUDE_AGENT_VERSION="$(jq -r '.agent_version' <<<"$cma_resolved_json")"
export CLAUDE_ENVIRONMENT_ID="$(jq -r '.environment_id' <<<"$cma_resolved_json")"
unset cma_resolved_json
