"""Application tool execution and result delivery, under the controller's context lease."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from managed_agents_app.cma_controller.custom_tools import (
    CONTROL_ERRORS,
    MAX_TOOL_BYTES,
    TOOL_FAILURE,
    TOOL_TIMEOUT_SECONDS,
    CustomToolRegistry,
    InvalidToolCall,
    ToolContext,
    ToolResult,
)
from managed_agents_app.cma_controller.pending_input import PendingInputStore
from managed_agents_app.cma_controller.provider import CmaProvider, DefinitiveProviderError
from managed_agents_app.cma_controller.runtime_repository import (
    ControllerRuntimeRepository,
    LostSchedulerClaim,
)
from managed_agents_app.cma_controller.tool_execution_repository import ToolExecutionRepository
from managed_agents_app.policy.models import (
    PolicyError,
    PolicyState,
    ToolAuthorization,
    ToolPolicyEngine,
    ToolPolicyRequest,
)

logger = logging.getLogger(__name__)


def call_digest(source: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {key: source.get(key) for key in ("id", "type", "name", "input")},
            sort_keys=True,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def result_digest(result: ToolResult) -> str:
    return hashlib.sha256(json.dumps([result.text, result.is_error]).encode()).hexdigest()


class AuthorizationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    invocation_id: str
    call_digest: str
    authorization: ToolAuthorization


class OutcomeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    invocation_id: str
    call_digest: str
    result: ToolResult


class AdmittedRecord(OutcomeRecord):
    raw_result_digest: str


class ToolExecutionRunner:
    def __init__(
        self,
        repository: ControllerRuntimeRepository,
        provider: CmaProvider,
        pending: PendingInputStore,
        registry: CustomToolRegistry,
    ) -> None:
        self.repository = repository
        self.executions = ToolExecutionRepository(repository)
        self.provider = provider
        self.pending = pending
        self.registry = registry

    def prepare(self, context_id: str, task_id: str, claim: str, source: dict[str, Any]) -> dict[str, Any]:
        code = None
        try:
            self.registry.prepare(source.get("name"), source.get("input"))
        except InvalidToolCall as error:
            code = error.code
        return self.executions.prepare(
            context_id, task_id, str(source["id"]), claim, call_digest(source), code
        )

    def store_authorization(self, row: dict[str, Any], authorization: ToolAuthorization) -> str:
        key = str(uuid.uuid4())
        self.pending.put(
            key,
            AuthorizationRecord(
                invocation_id=str(row["invocation_id"]),
                call_digest=str(row["call_digest"]),
                authorization=authorization,
            ).model_dump_json(),
        )
        return key

    def _assert_active(self, context_id: str, task_id: str, claim: str) -> None:
        context = self.repository.get_context(context_id)
        task = self.repository.get_task(task_id)
        if (
            not context
            or not task
            or str(context.get("scheduler_claim_id")) != claim
            or not context.get("scheduler_lease_expires_at")
            or context["scheduler_lease_expires_at"] <= datetime.now(UTC)
            or str(context.get("active_task_id")) != task_id
            or task.get("cancel_requested_at")
            or task["internal_state"] == "terminal"
        ):
            raise LostSchedulerClaim(context_id)

    def _hold(self, context_id: str, task_id: str, claim: str, reason: str) -> bool:
        self.repository.update_task(task_id, context_id, claim, held_reason=reason)
        logger.warning("cma_custom_tool_held", extra={"task_id": task_id, "reason_code": reason})
        return False

    @staticmethod
    def _verify_record(row: dict[str, Any], record: AuthorizationRecord | OutcomeRecord) -> None:
        if record.invocation_id != str(row["invocation_id"]) or record.call_digest != row["call_digest"]:
            raise PolicyError("tool-artifact-mismatch")

    async def _execute(
        self, row: dict[str, Any], source: dict[str, Any], context_id: str, claim: str
    ) -> ToolResult:
        tool, arguments = self.registry.prepare(source.get("name"), source.get("input"))
        task_id = str(row["task_id"])
        self._assert_active(context_id, task_id, claim)
        deadline = asyncio.get_running_loop().time() + TOOL_TIMEOUT_SECONDS

        async def watch() -> None:
            while True:
                await asyncio.sleep(0.2)
                self._assert_active(context_id, task_id, claim)

        handler = asyncio.ensure_future(
            tool.handler(arguments, ToolContext(str(row["invocation_id"]), deadline))
        )
        watcher = asyncio.create_task(watch())
        try:
            async with asyncio.timeout_at(deadline):
                done, _ = await asyncio.wait((handler, watcher), return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    await watcher
                result = await handler
            self._assert_active(context_id, task_id, claim)
            if not isinstance(result, ToolResult) or not isinstance(result.text, str):
                raise ValueError("Invalid handler result")
            if type(result.is_error) is not bool or (result.is_error and result.text != TOOL_FAILURE):
                raise ValueError("Private failure output is unsupported")
            return result
        finally:
            handler.cancel()
            watcher.cancel()
            await asyncio.gather(handler, watcher, return_exceptions=True)

    async def reconcile(
        self,
        context_id: str,
        task: dict[str, Any],
        claim: str,
        session_id: str,
        request: dict[str, Any],
        source: dict[str, Any] | None,
        events: list[dict[str, Any]],
        policy: ToolPolicyEngine | None,
        remaining: float,
    ) -> bool:
        """True means delivery is proven. Operational failures retain the execution marker."""
        task_id = str(task["task_id"])
        row = self.executions.get(task_id, str(request["cma_event_id"]))
        if row is None or source is None or row["call_digest"] != call_digest(source):
            return self._hold(context_id, task_id, claim, "tool-call-mismatch")
        cancelled = bool((self.repository.get_task(task_id) or task).get("cancel_requested_at"))
        try:
            return await self._reconcile(
                context_id,
                task_id,
                claim,
                session_id,
                request,
                source,
                events,
                policy,
                remaining,
                row,
                cancelled,
            )
        except LostSchedulerClaim, DefinitiveProviderError:
            raise
        except PolicyError as error:
            return self._hold(context_id, task_id, claim, f"tool-{error.code}")
        except Exception:
            # Provider/library exceptions may contain credentials, arguments or results.
            return self._hold(context_id, task_id, claim, "tool-recovery-unavailable")

    async def _reconcile(
        self,
        context_id: str,
        task_id: str,
        claim: str,
        session_id: str,
        request: dict[str, Any],
        source: dict[str, Any],
        events: list[dict[str, Any]],
        policy: ToolPolicyEngine | None,
        remaining: float,
        row: dict[str, Any],
        cancelled: bool,
    ) -> bool:
        if row["state"] == "resolved":
            return True
        started = asyncio.get_running_loop().time()

        def budget() -> float:
            return remaining - (asyncio.get_running_loop().time() - started)

        # An attempted delivery is immutable, including across Stop. Canonical proof
        # may settle it; neither a new payload nor a second send is safe.
        if cancelled and row.get("delivery_attempted_at") is None:
            row = self.executions.transition(
                row,
                context_id,
                claim,
                "admitted",
                cancelled=True,
                control_code="cancelled",
                result_digest=result_digest(ToolResult(CONTROL_ERRORS["cancelled"], True)),
            )
        elif row["state"] == "withheld":
            return self._hold(context_id, task_id, claim, "tool-result-withheld")
        elif row["state"] == "pending":
            code = row.get("control_code") or ("denied" if request["decision"] == "deny" else None)
            if code:
                row = self.executions.transition(
                    row,
                    context_id,
                    claim,
                    "admitted",
                    control_code=code,
                    result_digest=result_digest(ToolResult(CONTROL_ERRORS[str(code)], True)),
                )
            else:
                if request["decision"] != "allow" or not row.get("authorization_object_key"):
                    raise PolicyError("authorization-missing")
                # Validate evidence and handler availability before recording an attempt.
                auth = AuthorizationRecord.model_validate_json(
                    self.pending.get(str(row["authorization_object_key"]))
                )
                self._verify_record(row, auth)
                self.registry.prepare(source.get("name"), source.get("input"))
                if budget() < TOOL_TIMEOUT_SECONDS + 5:
                    return False
                row = self.executions.transition(
                    row, context_id, claim, "running", execution_attempted_at=datetime.now(UTC)
                )
                try:
                    result = await self._execute(row, source, context_id, claim)
                except LostSchedulerClaim:
                    raise
                except Exception:
                    return self._hold(context_id, task_id, claim, "tool-execution-uncertain")
                if len(result.text.encode("utf-8")) > MAX_TOOL_BYTES:
                    self.executions.transition(row, context_id, claim, "withheld")
                    return self._hold(context_id, task_id, claim, "tool-result-withheld")
                # The preallocated key recovers a successful put even if its ACK or
                # the following DB publication is lost. It is never overwritten.
                self.pending.put(
                    str(row["result_object_key"]),
                    OutcomeRecord(
                        invocation_id=str(row["invocation_id"]),
                        call_digest=str(row["call_digest"]),
                        result=result,
                    ).model_dump_json(),
                )

        if row["state"] == "running":
            try:
                outcome = OutcomeRecord.model_validate_json(self.pending.get(str(row["result_object_key"])))
            except Exception:
                return self._hold(context_id, task_id, claim, "tool-execution-uncertain")
            self._verify_record(row, outcome)
            row = self.executions.transition(
                row, context_id, claim, "result_ready", result_digest=result_digest(outcome.result)
            )

        if row["state"] == "result_ready":
            if row.get("control_code"):
                result = ToolResult(CONTROL_ERRORS[str(row["control_code"])], True)
            else:
                outcome = OutcomeRecord.model_validate_json(self.pending.get(str(row["result_object_key"])))
                self._verify_record(row, outcome)
                result = outcome.result
                if len(result.text.encode("utf-8")) > MAX_TOOL_BYTES or (
                    result.is_error and result.text != TOOL_FAILURE
                ):
                    raise PolicyError("invalid-result")
            if result_digest(result) != row["result_digest"]:
                raise PolicyError("result-mismatch")

            if budget() < 2:
                return False
            if policy is None:
                raise PolicyError()
            auth = AuthorizationRecord.model_validate_json(
                self.pending.get(str(row["authorization_object_key"]))
            )
            self._verify_record(row, auth)
            self._assert_active(context_id, task_id, claim)
            timeout = min(budget(), self.repository.config.openappa_timeout_seconds)
            async with asyncio.timeout(timeout):
                admitted = await policy.evaluate_tool_result(
                    ToolPolicyRequest(
                        context_id,
                        str(source["id"]),
                        self.repository.config.app_name,
                        "custom",
                        str(source["name"]),
                        source["input"],
                        state=PolicyState.from_context(self.repository.get_context(context_id) or {}),
                    ),
                    auth.authorization,
                    result.text,
                    result.is_error,
                    timeout,
                )
            if (self.repository.get_context(context_id) or {}).get(
                "policy_checkpoint_key"
            ) and admitted.checkpoint is None:
                raise PolicyError("policy-checkpoint-missing")
            if admitted.decision not in {"deliver", "withhold"}:
                raise PolicyError("invalid-response")
            fields: dict[str, Any] = {}
            if admitted.decision == "deliver":
                if not isinstance(admitted.text, str) or len(admitted.text.encode()) > MAX_TOOL_BYTES:
                    raise PolicyError("invalid-admitted-result")
                if result.is_error and admitted.text != TOOL_FAILURE:
                    raise PolicyError("invalid-admitted-result")
                result = ToolResult(admitted.text, result.is_error)
                key = str(uuid.uuid4())
                self.pending.put(
                    key,
                    AdmittedRecord(
                        invocation_id=str(row["invocation_id"]),
                        call_digest=str(row["call_digest"]),
                        raw_result_digest=str(row["result_digest"]),
                        result=result,
                    ).model_dump_json(),
                )
                fields = {"admitted_result_object_key": key, "admitted_result_digest": result_digest(result)}
            row = self.executions.transition(
                row,
                context_id,
                claim,
                "admitted" if admitted.decision == "deliver" else "withheld",
                checkpoint=admitted.checkpoint,
                **fields,
            )
            if row["state"] == "withheld":
                return self._hold(context_id, task_id, claim, "tool-result-withheld")

        # After admission, only the committed admitted artifact is deliverable.
        # Neither a missing object nor a lost acknowledgment permits a raw fallback.
        if row.get("control_code"):
            result = ToolResult(CONTROL_ERRORS[str(row["control_code"])], True)
            if result_digest(result) != row["result_digest"]:
                raise PolicyError("result-mismatch")
        else:
            admitted_key = row.get("admitted_result_object_key")
            if not admitted_key:
                raise PolicyError("admitted-result-missing")
            output = AdmittedRecord.model_validate_json(self.pending.get(str(admitted_key)))
            self._verify_record(row, output)
            result = output.result
            if (
                output.raw_result_digest != row["result_digest"]
                or result_digest(result) != row.get("admitted_result_digest")
                or len(result.text.encode()) > MAX_TOOL_BYTES
                or (result.is_error and result.text != TOOL_FAILURE)
            ):
                raise PolicyError("admitted-result-mismatch")

        def accepted(history: list[dict[str, Any]]) -> bool:
            return any(
                event.get("type") == "user.custom_tool_result"
                and event.get("custom_tool_use_id") == request["cma_event_id"]
                and event.get("content") == [{"type": "text", "text": result.text}]
                and event.get("is_error", False) is result.is_error
                for event in history
            )

        if not accepted(events):
            if row.get("delivery_attempted_at") is not None:
                return self._hold(context_id, task_id, claim, "tool-delivery-uncertain")
            if budget() < 2:
                return False
            row = self.executions.transition(
                row,
                context_id,
                claim,
                "delivering",
                cancelled=cancelled,
                delivery_attempted_at=datetime.now(UTC),
            )
            try:
                async with asyncio.timeout(budget()):
                    await self.provider.send_custom_tool_result(
                        session_id, str(request["cma_event_id"]), result.text, is_error=result.is_error
                    )
            except DefinitiveProviderError:
                raise
            except Exception:
                if not accepted(await self.provider.list_events(session_id)):
                    return self._hold(context_id, task_id, claim, "tool-delivery-uncertain")
        # Cancellation may have raced the external acknowledgment. This is receipt
        # reconciliation only; it cannot initiate a new call or deliver another result.
        cancelled = bool((self.repository.get_task(task_id) or {}).get("cancel_requested_at"))
        self.executions.transition(row, context_id, claim, "resolved", cancelled=cancelled)
        return True

    def cleanup(self, task_id: str, event_id: str) -> None:
        row = self.executions.get(task_id, event_id)
        if row and row["state"] == "resolved":
            for field in ("result_object_key", "authorization_object_key", "admitted_result_object_key"):
                if row.get(field):
                    try:
                        self.pending.delete(str(row[field]))
                    except Exception:
                        logger.warning("cma_custom_tool_cleanup_deferred", extra={"task_id": task_id})
