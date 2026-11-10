"""Native embedded OpenAPPA adapter; never interprets authorization rules."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from managed_agents_app.policy.models import (
    HumanReview,
    PolicyCheckpoint,
    PolicyError,
    ToolAuthorization,
    ToolPolicyDecision,
    ToolPolicyRequest,
    ToolResultDecision,
)
from managed_agents_app.policy.snapshots import SnapshotStore, snapshot


class OpenAppaPolicyEngine:
    def __init__(
        self,
        binary: str,
        policy_path: str,
        *,
        custom_tools: tuple[str, ...] = (),
        snapshots: SnapshotStore | None = None,
    ) -> None:
        self.binary = binary
        self.policy_path = policy_path
        self.custom_tools = custom_tools
        self.snapshots = snapshots

    async def evaluate_tool_call(self, request: ToolPolicyRequest, timeout: float) -> ToolPolicyDecision:
        return await self._call(request, timeout)

    async def resolve_tool_call(
        self,
        request: ToolPolicyRequest,
        review: HumanReview,
        ruling: Literal["allow", "deny"],
        timeout: float,
    ) -> ToolPolicyDecision:
        result = await self._call(request, timeout, review, ruling)
        if result.decision == "human_review" or (ruling == "deny" and result.decision != "deny"):
            raise PolicyError("policy-invalid-response")
        return result

    @staticmethod
    def _resolution(review: HumanReview | None, ruling: str | None) -> dict[str, str] | None:
        if review is None:
            if ruling is not None:
                raise PolicyError("policy-invalid-request")
            return None
        if ruling not in {"allow", "deny"}:
            raise PolicyError("policy-invalid-request")
        return {
            "fingerprint": review.fingerprint,
            "authority": review.authority,
            "ruling": "approve" if ruling == "allow" else "deny",
        }

    async def _call(
        self,
        request: ToolPolicyRequest,
        timeout: float,
        review: HumanReview | None = None,
        ruling: Literal["allow", "deny"] | None = None,
    ) -> ToolPolicyDecision:
        try:
            policy = Path(self.policy_path).read_text()
        except OSError as error:
            raise PolicyError() from error
        result, checkpoint = await self._exchange(request, timeout, policy, self._resolution(review, ruling))
        try:
            decision = result["decision"]
            if decision == "human_review":
                if set(result) != {"decision", "reason", "review"}:
                    raise ValueError("Unexpected review result")
                offered = result["review"]
                if (
                    set(offered) != {"authority", "fingerprint", "description"}
                    or not all(isinstance(v, str) and v for v in offered.values())
                    or not re.fullmatch(r"[a-f0-9]{64}", offered["fingerprint"])
                ):
                    raise ValueError("Invalid review")
                return ToolPolicyDecision(
                    "human_review", review=HumanReview(**offered), checkpoint=checkpoint
                )
            fields = {"decision", "reason"}
            authorization = None
            if request.tool_kind == "custom" and decision == "allow":
                fields.add("fingerprint")
                if not isinstance(result.get("fingerprint"), str) or not re.fullmatch(
                    r"[a-f0-9]{64}", result["fingerprint"]
                ):
                    raise ValueError("Invalid authorization")
                authorization = ToolAuthorization(policy, result["fingerprint"], review, ruling)
            if set(result) != fields or decision not in {"allow", "deny"}:
                raise ValueError("Unknown decision")
            return ToolPolicyDecision(
                decision,
                "Denied by OpenAPPA policy." if decision == "deny" else None,
                authorization=authorization,
                checkpoint=checkpoint,
            )
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise PolicyError("policy-invalid-response") from error

    async def evaluate_tool_result(
        self,
        request: ToolPolicyRequest,
        authorization: ToolAuthorization,
        text: str,
        is_error: bool,
        timeout: float,
    ) -> ToolResultDecision:
        result, checkpoint = await self._exchange(
            request,
            timeout,
            authorization.policy,
            self._resolution(authorization.review, authorization.ruling),
            result={"text": text, "is_error": is_error},
            authorization=authorization.fingerprint,
        )
        if result.get("decision") == "deliver":
            if (
                set(result) != {"decision", "text"}
                or not isinstance(result.get("text"), str)
                or len(result["text"].encode("utf-8")) > 65536
                or (is_error and result["text"] != text)
            ):
                raise PolicyError("policy-invalid-response")
        elif result != {"decision": "withhold"}:
            raise PolicyError("policy-invalid-response")
        return ToolResultDecision(result["decision"], checkpoint, result.get("text"))

    async def _exchange(
        self,
        request: ToolPolicyRequest,
        timeout: float,
        policy: str,
        resolve: dict[str, str] | None,
        *,
        result: dict[str, Any] | None = None,
        authorization: str | None = None,
    ) -> tuple[dict[str, Any], PolicyCheckpoint | None]:
        if self.snapshots is None:
            # Isolated native tests may still exercise the stateless adapter.
            return await self._process(
                request, timeout, policy, resolve, result=result, authorization=authorization
            ), None
        state = request.state
        if (
            state is None
            or (state.revision == 0) != (state.key is None)
            or (state.key is None) != (state.policy_digest is None)
        ):
            raise PolicyError("policy-checkpoint-invalid")
        digest = hashlib.sha256(policy.encode()).hexdigest()
        if state.policy_digest is not None and digest != state.policy_digest:
            raise PolicyError("policy-version-changed")
        try:
            async with asyncio.timeout(timeout):
                with tempfile.TemporaryDirectory(prefix="openappa-") as directory:
                    database = Path(directory) / "state.sqlite"
                    if state.key is not None:
                        database.write_bytes(await asyncio.to_thread(self.snapshots.get, state.key))
                    payload = await self._process(
                        request,
                        timeout,
                        policy,
                        resolve,
                        result=result,
                        authorization=authorization,
                        database=str(database),
                    )
                    body = snapshot(database, Path(directory) / "snapshot.sqlite")
                    # Never republish an old orphan that a concurrent sweep could delete.
                    key = uuid.uuid4().hex + "-" + hashlib.sha256(body).hexdigest()
                    await asyncio.to_thread(self.snapshots.put, key, body)
                    return payload, PolicyCheckpoint(state.revision, key, digest)
        except PolicyError:
            raise
        except TimeoutError as error:
            raise PolicyError("policy-runtime-timeout") from error
        except Exception as error:
            raise PolicyError("policy-checkpoint-unavailable") from error

    async def _process(
        self,
        request: ToolPolicyRequest,
        timeout: float,
        policy: str,
        resolve: dict[str, str] | None,
        *,
        result: dict[str, Any] | None = None,
        authorization: str | None = None,
        database: str | None = None,
    ) -> dict[str, Any]:
        try:
            body = json.dumps(
                {
                    "version": 6,
                    "policy": policy,
                    "request": {k: v for k, v in asdict(request).items() if k != "state"},
                    "database": database,
                    "resolve": resolve,
                    "custom_tools": self.custom_tools,
                    "result": result,
                    "authorization": authorization,
                }
            ).encode()
            if len(body) > 2 * 1024 * 1024:
                raise PolicyError("policy-invalid-request")
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
                if len(chunks) > 512 * 1024:
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
            if process.returncode or response.get("version") != 6:
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
            payload = response["result"]
            if not isinstance(payload, dict):
                raise ValueError("Invalid result")
            return payload
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise PolicyError("policy-invalid-response") from error
