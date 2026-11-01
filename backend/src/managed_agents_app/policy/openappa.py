"""Native embedded OpenAPPA adapter; never interprets authorization rules."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from managed_agents_app.policy.models import HumanReview, PolicyError, ToolPolicyDecision, ToolPolicyRequest


class OpenAppaPolicyEngine:
    def __init__(self, binary: str, policy_path: str) -> None:
        self.binary = binary
        self.policy_path = policy_path

    async def evaluate_tool_call(self, request: ToolPolicyRequest, timeout: float) -> ToolPolicyDecision:
        return await self._exchange(request, timeout)

    async def resolve_tool_call(
        self,
        request: ToolPolicyRequest,
        review: HumanReview,
        ruling: Literal["allow", "deny"],
        timeout: float,
    ) -> ToolPolicyDecision:
        result = await self._exchange(
            request,
            timeout,
            {
                "fingerprint": review.fingerprint,
                "authority": review.authority,
                "ruling": "approve" if ruling == "allow" else "deny",
            },
        )
        if result.decision == "human_review" or (ruling == "deny" and result.decision != "deny"):
            raise PolicyError("policy-invalid-response")
        return result

    async def _exchange(
        self, request: ToolPolicyRequest, timeout: float, resolve: dict[str, Any] | None = None
    ) -> ToolPolicyDecision:
        try:
            body = json.dumps(
                {
                    "version": 3,
                    "policy": Path(self.policy_path).read_text(),
                    "request": asdict(request),
                    "resolve": resolve,
                }
            ).encode()
            process = await asyncio.create_subprocess_exec(
                self.binary,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError) as error:
            raise PolicyError() from error

        async def exchange() -> bytes:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(body)
            await process.stdin.drain()
            process.stdin.close()
            chunks = bytearray()
            while chunk := await process.stdout.read(4096):
                chunks.extend(chunk)
                if len(chunks) > 65536:
                    raise PolicyError("policy-invalid-response")
            await process.wait()
            return bytes(chunks)

        try:
            stdout = await asyncio.wait_for(exchange(), timeout=timeout)
        except BaseException as error:
            if process.returncode is None:
                process.kill()
            await process.wait()
            if isinstance(error, TimeoutError):
                raise PolicyError("policy-runtime-timeout") from error
            raise
        try:
            response = json.loads(stdout)
            if process.returncode or response.get("version") != 3:
                raise PolicyError("policy-runtime-failure")
            if "error" in response:
                if set(response) != {"version", "error"}:
                    raise ValueError("Unexpected error response")
                codes = {
                    "invalid_policy": "policy-invalid-policy",
                    "unsupported_policy": "policy-unsupported-policy",
                    "invalid_request": "policy-invalid-request",
                    "unsupported_remedy": "policy-remedy-unsupported",
                    "stale_review": "policy-stale-review",
                }
                raise PolicyError(codes.get(response["error"], "policy-runtime-failure"))
            if set(response) != {"version", "result"}:
                raise ValueError("Unexpected response")
            result = response["result"]
            decision = result["decision"]
            if decision == "human_review":
                if set(result) != {"decision", "reason", "review"}:
                    raise ValueError("Unexpected review result")
                review = result["review"]
                if (
                    set(review) != {"authority", "fingerprint", "description"}
                    or not all(isinstance(v, str) and v for v in review.values())
                    or not re.fullmatch(r"[a-f0-9]{64}", review["fingerprint"])
                ):
                    raise ValueError("Invalid review")
                return ToolPolicyDecision("human_review", review=HumanReview(**review))
            if set(result) != {"decision", "reason"} or decision not in {"allow", "deny"}:
                raise ValueError("Unknown decision")
            # Do not expose arbitrary runtime feedback, which may include arguments.
            return ToolPolicyDecision(decision, "Denied by OpenAPPA policy." if decision == "deny" else None)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise PolicyError("policy-invalid-response") from error
