"""One bounded, fenced reconciliation step for a CMA context."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal

from managed_agents_app.cma_controller.custom_tools import CustomToolRegistry, default_custom_tools
from managed_agents_app.cma_controller.pending_input import PendingInputStore
from managed_agents_app.cma_controller.preview import PreviewHint, PreviewTrigger
from managed_agents_app.cma_controller.provider import CmaProvider, DefinitiveProviderError
from managed_agents_app.cma_controller.push_trigger import PushTrigger
from managed_agents_app.cma_controller.runtime_repository import (
    ControllerRuntimeRepository,
    LostSchedulerClaim,
)
from managed_agents_app.cma_controller.tool_execution import ToolExecutionRunner
from managed_agents_app.cma_controller.trigger import SchedulerTrigger
from managed_agents_app.observability import emit
from managed_agents_app.policy.models import (
    HumanReview,
    PolicyError,
    PolicyState,
    ToolPolicyEngine,
    ToolPolicyRequest,
)
from managed_agents_app.policy.snapshots import SnapshotStore

logger = logging.getLogger(__name__)
_preview_hints: ContextVar[dict[str, PreviewHint] | None] = ContextVar("preview_hints", default=None)
_deadline: ContextVar[float | None] = ContextVar("scheduler_deadline", default=None)


def approval_request_id(task_id: str, provider_event_id: str) -> str:
    """Stable opaque identity; provider event IDs remain controller-private."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"human-input:{task_id}:{provider_event_id}"))


class SchedulerBusy(RuntimeError):
    """A live worker owns the context; SQS must retry the wakeup."""


def _after(events: list[dict[str, Any]], event_id: str | None) -> list[dict[str, Any]]:
    if not event_id:
        return events
    for index, event in enumerate(events):
        if event.get("id") == event_id:
            return events[index + 1 :]
    return []


def _latest_status(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (
            event
            for event in reversed(events)
            if str(event.get("type", "")).startswith(("session.status_", "session.thread_status_"))
            or event.get("type") == "session.budget_reached"
        ),
        None,
    )


class CmaScheduler:
    def __init__(
        self,
        repository: ControllerRuntimeRepository,
        provider: CmaProvider,
        pending_input: PendingInputStore,
        trigger: SchedulerTrigger,
        push_trigger: PushTrigger | None = None,
        *,
        policy_engine: ToolPolicyEngine | None = None,
        preview_trigger: PreviewTrigger | None = None,
        custom_tools: CustomToolRegistry | None = None,
        snapshots: SnapshotStore | None = None,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.pending_input = pending_input
        self.trigger = trigger
        self.push_trigger = push_trigger
        self.policy_engine = policy_engine
        self.snapshots = snapshots
        self.preview_trigger = preview_trigger
        self.custom_tools = ToolExecutionRunner(
            repository, provider, pending_input, custom_tools or default_custom_tools()
        )

    def sweep_orphans(self) -> tuple[int, int]:
        referenced = self.repository.pending_input_keys()
        deleted = 0
        for key in self.pending_input.list_old_keys(86400):
            if key not in referenced:
                self.pending_input.delete(key)
                deleted += 1
        if self.snapshots is not None:
            checkpoints = self.repository.policy_checkpoint_keys()
            for key in self.snapshots.list_old_keys(86400):
                if key not in checkpoints:
                    self.snapshots.delete(key)
                    deleted += 1
        stale = self.repository.stale_pending_input_count()
        if stale:
            logger.warning("cma_stale_pending_input", extra={"count": stale})
        return deleted, stale

    async def run_once(self, context_id: str, *, remaining_seconds: float = 60) -> None:
        deadline_token = _deadline.set(asyncio.get_running_loop().time() + remaining_seconds - 5)
        try:
            await self._run_once(context_id)
        finally:
            _deadline.reset(deadline_token)

    def _remaining(self, context_id: str) -> float:
        context = self.repository.get_context(context_id)
        if context is None or not context.get("scheduler_lease_expires_at"):
            return 0
        lease = float((context["scheduler_lease_expires_at"] - datetime.now(UTC)).total_seconds()) - 5
        deadline = _deadline.get()
        return min(lease, deadline - asyncio.get_running_loop().time()) if deadline is not None else lease

    async def _run_once(self, context_id: str) -> None:
        claim = self.repository.claim_scheduler(context_id)
        if claim is None:
            emit("scheduler_claim", context_id=context_id, outcome="busy")
            raise SchedulerBusy(context_id)
        emit("scheduler_claim", context_id=context_id, outcome="acquired")
        hints: dict[str, PreviewHint] = {}
        token = _preview_hints.set(hints)
        try:
            await self._reconcile(context_id, claim)
        finally:
            self.repository.release_scheduler(context_id, claim)
            emit("scheduler_released", context_id=context_id)
            _preview_hints.reset(token)
            if self.push_trigger is not None:
                try:
                    self.push_trigger.schedule_context(context_id, "scheduler-reconciled")
                except Exception:
                    logger.exception("cma_push_wakeup_failed", extra={"context_id": context_id})
            # P0 commits and releases its fence before this bounded, disposable hint.
            if self.preview_trigger is not None:
                for hint in hints.values():
                    try:
                        self.preview_trigger.start(hint)
                    except Exception as error:
                        emit("preview_hint_failed", task_id=hint.task_id, error_type=type(error).__name__)

    def _preview(self, task_id: str, context_id: str, phase_id: str) -> None:
        if self.preview_trigger is None or (hints := _preview_hints.get()) is None:
            return
        try:
            context = self.repository.get_context(context_id)
            task = self.repository.get_task(task_id)
            if (
                context
                and task
                and context.get("cma_session_id")
                and str(context.get("active_task_id")) == task_id
                and not task.get("cancel_requested_at")
                and task["internal_state"] != "terminal"
            ):
                hints[task_id] = PreviewHint(task_id, context_id, str(context["cma_session_id"]), phase_id)
        except Exception as error:
            emit("preview_hint_failed", task_id=task_id, error_type=type(error).__name__)

    def _resolve_input(self, request: dict[str, Any], context_id: str, claim: str) -> None:
        request_id, task_id = str(request["request_id"]), str(request["task_id"])
        self.repository.resolve_input_request(request_id, context_id, claim)
        if self.preview_trigger is not None:
            try:
                current = self.repository.get_input_request(request_id, task_id)
                if current and current["status"] == "resolved":
                    self._preview(task_id, context_id, request_id)
            except Exception as error:
                emit("preview_hint_failed", task_id=task_id, error_type=type(error).__name__)

    async def _reconcile(self, context_id: str, claim: str) -> None:
        context = self.repository.get_context(context_id)
        if context is None:
            return
        active_id = context.get("active_task_id")
        if active_id:
            task = self.repository.get_task(str(active_id))
            if task is not None:
                await self._reconcile_active(context, task, claim)
        else:
            task = self.repository.next_queued(context_id)
            if task is not None:
                if context["lifecycle_state"] == "failed":
                    self._finish(context_id, str(task["task_id"]), claim, "FAILED", "")
                else:
                    events = (
                        await self.provider.list_events(str(context["cma_session_id"]))
                        if context.get("cma_session_id")
                        else []
                    )
                    predecessor = str(events[-1]["id"]) if events and events[-1].get("id") else None
                    task = self.repository.activate_task(context_id, str(task["task_id"]), claim, predecessor)
                    await self._dispatch(context, task, claim)

        refreshed = self.repository.get_context(context_id)
        if refreshed and refreshed.get("active_task_id"):
            self.trigger.schedule(context_id, "reconcile-active", 30)
            emit("scheduler_wakeup", context_id=context_id, delay_seconds=30, reason="reconcile-active")
        elif self.repository.next_queued(context_id):
            self.trigger.schedule(context_id, "next-queued")

    async def _dispatch(self, context: dict[str, Any], task: dict[str, Any], claim: str) -> None:
        context_id = str(context["context_id"])
        task_id = str(task["task_id"])
        key = str(task["input_object_key"])
        text = self.pending_input.get(key)
        if not context.get("cma_session_id"):
            recovered = await self.provider.find_session(context_id, str(context["creation_message_id"]))
            try:
                session = recovered or await self.provider.create_session(
                    context_id, str(context["creation_message_id"]), text
                )
            except DefinitiveProviderError as error:
                self.repository.update_context(context_id, claim, lifecycle_state="failed")
                self._finish(context_id, task_id, claim, "FAILED", "")
                logger.error("cma_create_failed", extra={"error_type": type(error).__name__})
                return
            except Exception as error:
                self.repository.update_task(task_id, context_id, claim, held_reason="ambiguous-create")
                logger.error(
                    "cma_ambiguous_create",
                    extra={"context_id": context_id, "task_id": task_id, "error_type": type(error).__name__},
                )
                return
            self.repository.update_context(
                context_id, claim, cma_session_id=session.id, lifecycle_state="ready"
            )
            events = await self.provider.list_events(session.id)
            input_event = next((e for e in events if e.get("type") == "user.message"), None)
            if input_event and input_event.get("id"):
                self._bind_input(task, context_id, claim, str(input_event["id"]))
                await self._reconcile_active(
                    self.repository.get_context(context_id) or context,
                    self.repository.get_task(task_id) or task,
                    claim,
                )
            else:
                self.repository.update_task(
                    task_id, context_id, claim, held_reason="initial-input-not-visible"
                )
            return

        session_id = str(context["cma_session_id"])
        try:
            input_event_id = await self.provider.send_message(session_id, text)
        except DefinitiveProviderError as error:
            self._finish(context_id, task_id, claim, "FAILED", "")
            logger.error("cma_send_failed", extra={"task_id": task_id, "error_type": type(error).__name__})
            return
        except Exception as error:
            self.repository.update_task(task_id, context_id, claim, held_reason="ambiguous-send")
            logger.error(
                "cma_ambiguous_send",
                extra={"context_id": context_id, "task_id": task_id, "error_type": type(error).__name__},
            )
            return
        if input_event_id:
            self._bind_input(task, context_id, claim, input_event_id)
        else:
            self.repository.update_task(task_id, context_id, claim, held_reason="input-event-not-returned")

    def _bind_input(self, task: dict[str, Any], context_id: str, claim: str, event_id: str) -> None:
        self.repository.update_task(
            str(task["task_id"]),
            context_id,
            claim,
            cma_input_event_id=event_id,
            input_object_key=None,
            internal_state="waiting",
            a2a_state="WORKING",
            held_reason=None,
        )
        self._preview(str(task["task_id"]), context_id, event_id)
        if key := task.get("input_object_key"):
            try:
                self.pending_input.delete(str(key))
            except Exception as error:
                logger.error(
                    "cma_pending_input_delete_failed",
                    extra={"task_id": task["task_id"], "error_type": type(error).__name__},
                )

    async def _reconcile_active(self, context: dict[str, Any], task: dict[str, Any], claim: str) -> None:
        context_id = str(context["context_id"])
        task_id = str(task["task_id"])
        session_id = context.get("cma_session_id")
        if not session_id:
            recovered = await self.provider.find_session(context_id, str(context["creation_message_id"]))
            if recovered is None:
                self.repository.update_task(task_id, context_id, claim, held_reason="ambiguous-create")
                logger.warning("cma_ambiguous_create_held", extra={"task_id": task_id})
                return
            self.repository.update_context(
                context_id, claim, cma_session_id=recovered.id, lifecycle_state="ready"
            )
            session_id = recovered.id
        events = await self.provider.list_events(str(session_id))
        if not task.get("cma_input_event_id"):
            candidates = [
                e
                for e in _after(events, task.get("cma_predecessor_event_id"))
                if e.get("type") == "user.message"
            ]
            if not candidates:
                self.repository.update_task(task_id, context_id, claim, held_reason="ambiguous-dispatch")
                logger.warning("cma_ambiguous_dispatch_held", extra={"task_id": task_id})
                return
            self._bind_input(task, context_id, claim, str(candidates[0]["id"]))
            task = self.repository.get_task(task_id) or task

        if task.get("cancel_requested_at"):
            await self._reconcile_cancel(context_id, task, claim, str(session_id), events)
            return

        if await self._reconcile_confirmations(context_id, task, claim, str(session_id), events):
            return
        events = await self.provider.list_events(str(session_id))
        relevant = _after(events, str(task["cma_input_event_id"]))
        status = _latest_status(relevant)
        if status is None:
            return
        kind = str(status.get("type")).replace("thread_status", "status")
        unresolved = any(
            request["status"] != "resolved" for request in self.repository.list_input_requests(task_id)
        )
        if unresolved and (
            kind == "session.status_running"
            or (
                kind == "session.status_idle"
                and (status.get("stop_reason") or {}).get("type") != "requires_action"
            )
        ):
            # Previously discovered approvals survive partial provider resume. A later
            # running/end-turn status cannot erase requests deferred by the lease.
            if not await self._evaluate_policy(
                context_id, task_id, claim, {str(e.get("id")): e for e in events}
            ):
                self._set_waiting_state(context_id, task_id, claim)
                return
            refreshed_task = self.repository.get_task(task_id) or task
            if not refreshed_task.get("cancel_requested_at"):
                if await self._reconcile_confirmations(
                    context_id, refreshed_task, claim, str(session_id), events
                ):
                    return
                self._set_waiting_state(context_id, task_id, claim)
            return
        if kind in {"session.status_terminated", "session.budget_reached"}:
            self.repository.update_context(context_id, claim, lifecycle_state="failed")
            self._finish(context_id, task_id, claim, "FAILED", str(status.get("id", "")))
        elif kind == "session.status_idle":
            stop_reason = status.get("stop_reason") or {}
            if stop_reason.get("type") == "requires_action":
                ids = {str(item) for item in stop_reason.get("event_ids", [])}
                by_id = {str(event.get("id")): event for event in events}
                for source in events:
                    event_id = str(source.get("id"))
                    if event_id not in ids:
                        continue
                    if source and source.get("type") in {"agent.tool_use", "agent.mcp_tool_use"}:
                        self.repository.add_input_request(
                            approval_request_id(task_id, event_id), task_id, event_id, context_id, claim
                        )
                    elif (
                        source
                        and source.get("type") == "agent.custom_tool_use"
                        and source.get("name") == "ask_user"
                        and isinstance(source.get("input"), dict)
                        and isinstance(source["input"].get("prompt"), str)
                        and source["input"]["prompt"]
                    ):
                        self.repository.add_clarification_request(
                            approval_request_id(task_id, event_id), task_id, event_id, context_id, claim
                        )
                    elif source.get("type") == "agent.custom_tool_use" and source.get("name") != "ask_user":
                        self.repository.add_input_request(
                            approval_request_id(task_id, event_id), task_id, event_id, context_id, claim
                        )
                        self.custom_tools.prepare(context_id, task_id, claim, source)
                if not await self._evaluate_policy(context_id, task_id, claim, by_id):
                    self._set_waiting_state(context_id, task_id, claim)
                    return
                refreshed_task = self.repository.get_task(task_id) or task
                if not refreshed_task.get("cancel_requested_at"):
                    if await self._reconcile_confirmations(
                        context_id, refreshed_task, claim, str(session_id), events
                    ):
                        return
                    self._set_waiting_state(context_id, task_id, claim)
            elif stop_reason.get("type") in {"end_turn", "budget_reached"}:
                state = "FAILED" if stop_reason.get("type") == "budget_reached" else "COMPLETED"
                if state == "FAILED":
                    self.repository.update_context(context_id, claim, lifecycle_state="failed")
                self._finish(context_id, task_id, claim, state, str(status.get("id", "")))
        elif kind == "session.status_running":
            self.repository.update_task(
                task_id, context_id, claim, internal_state="waiting", a2a_state="WORKING"
            )

    def _set_waiting_state(self, context_id: str, task_id: str, claim: str) -> None:
        human_pending = any(
            (row["kind"] == "clarification" and row["status"] != "resolved")
            or (row.get("review_digest") and not row.get("human_ruling") and row["status"] == "pending")
            for row in self.repository.list_input_requests(task_id)
        )
        self.repository.update_task(
            task_id,
            context_id,
            claim,
            internal_state="input_required" if human_pending else "waiting",
            a2a_state="INPUT_REQUIRED" if human_pending else "WORKING",
        )

    async def _evaluate_policy(
        self, context_id: str, task_id: str, claim: str, by_id: dict[str, dict[str, Any]]
    ) -> bool:
        rows = self.repository.list_input_requests(task_id)
        # Provider history order is stable even when requires_action uses an unordered collection.
        order = {event_id: index for index, event_id in enumerate(by_id)}
        rows.sort(key=lambda row: order.get(str(row["cma_event_id"]), len(order)))
        for row in rows:
            # A prior custom result must change native state before the next call is authorized.
            source = by_id.get(str(row["cma_event_id"]))
            if (
                self.snapshots is not None
                and row["status"] == "resolving"
                and source
                and source.get("type") == "agent.custom_tool_use"
                and source.get("name") != "ask_user"
            ):
                return True
            if row["kind"] == "clarification" or row["status"] != "pending":
                continue
            current = self.repository.get_context(context_id)
            if current is None:
                return False
            task = self.repository.get_task(task_id)
            if task is None or task.get("cancel_requested_at"):
                return False
            remaining = self._remaining(context_id)
            if remaining <= 1:
                return True
            source = by_id.get(str(row["cma_event_id"]))
            fields = {
                "context_id": context_id,
                "task_id": task_id,
                "openappa_root_id": context_id,
                "openappa_call_id": str(row["cma_event_id"]),
            }
            try:
                execution = None
                if source and source.get("type") == "agent.custom_tool_use":
                    execution = self.custom_tools.prepare(context_id, task_id, claim, source)
                    if execution.get("control_code"):
                        self.repository.record_policy_decision(
                            context_id, task_id, claim, str(row["request_id"]), "deny"
                        )
                        continue
                if self.policy_engine is None:
                    raise PolicyError()
                if not source or not isinstance(source.get("name"), str):
                    raise PolicyError("policy-invalid-request")
                arguments = source.get("input")
                if arguments is None:
                    arguments = {}
                if not isinstance(arguments, dict):
                    raise PolicyError("policy-invalid-request")
                kind: Literal["builtin", "mcp", "custom"] = (
                    "custom"
                    if execution is not None
                    else "mcp"
                    if source["type"] == "agent.mcp_tool_use"
                    else "builtin"
                )
                name = str(source["name"])
                if kind == "mcp" and not name.startswith("mcp/"):
                    server = source.get("mcp_server_name")
                    if not isinstance(server, str) or not server:
                        raise PolicyError("policy-invalid-request")
                    name = f"mcp/{server}/{name}"
                request = ToolPolicyRequest(
                    context_id,
                    str(row["cma_event_id"]),
                    self.repository.config.app_name,
                    kind,
                    name,
                    arguments,
                    state=PolicyState.from_context(current),
                )
                fields.update(tool_name=name, tool_kind=kind)
                logger.info("policy_tool_call_started", extra=fields)
                async with asyncio.timeout(min(remaining, self.repository.config.openappa_timeout_seconds)):
                    if row.get("human_ruling"):
                        decision = await self.policy_engine.resolve_tool_call(
                            request,
                            HumanReview(str(row["review_authority"]), str(row["review_digest"]), ""),
                            row["human_ruling"],
                            remaining,
                        )
                    else:
                        decision = await self.policy_engine.evaluate_tool_call(request, remaining)
                if self.snapshots is not None and decision.checkpoint is None:
                    raise PolicyError("policy-checkpoint-missing")
                if decision.decision == "human_review":
                    if decision.review is None:
                        raise PolicyError("policy-invalid-response")
                    if decision.review.fingerprint != row.get("review_digest"):
                        key = str(uuid.uuid4())
                        self.pending_input.put(key, decision.review.description)
                        # A failed/ambiguous publication leaves only an orphan object,
                        # cleaned by the existing sweep, never a reusable consent.
                        self.repository.record_human_review(
                            context_id,
                            task_id,
                            claim,
                            str(row["request_id"]),
                            decision.review.fingerprint,
                            decision.review.authority,
                            key,
                            checkpoint=decision.checkpoint,
                        )
                    self.repository.update_task(task_id, context_id, claim, held_reason=None)
                    if self.snapshots is not None:
                        return True
                    continue
                if row.get("review_digest") and not row.get("human_ruling"):
                    # A changed policy must invalidate the offered review first.
                    self.repository.record_human_review(
                        context_id, task_id, claim, str(row["request_id"]), None, None, None
                    )
                    continue
                if decision.decision not in {"allow", "deny"}:
                    raise PolicyError("policy-invalid-response")
                try:
                    authorization_key = None
                    if execution is not None and decision.decision == "allow":
                        if decision.authorization is None:
                            raise PolicyError("policy-authorization-missing")
                        authorization_key = self.custom_tools.store_authorization(
                            execution, decision.authorization
                        )
                    self.repository.record_policy_decision(
                        context_id,
                        task_id,
                        claim,
                        str(row["request_id"]),
                        decision.decision,
                        checkpoint=decision.checkpoint,
                        **({"authorization_object_key": authorization_key} if execution is not None else {}),
                    )
                except LostSchedulerClaim:
                    raise
                except Exception as error:
                    raise PolicyError("policy-publication-failure") from error
                self.repository.update_task(task_id, context_id, claim, held_reason=None)
                logger.info(
                    "policy_tool_call_allowed" if decision.decision == "allow" else "policy_tool_call_denied",
                    extra={**fields, "policy_decision": decision.decision},
                )
                if self.snapshots is not None and execution is not None:
                    return True
            except LostSchedulerClaim:
                raise
            except Exception as error:
                reason = (
                    error.code
                    if isinstance(error, PolicyError)
                    else (
                        "policy-runtime-timeout" if isinstance(error, TimeoutError) else "policy-unavailable"
                    )
                )
                if reason == "policy-stale-review" or (
                    row.get("review_digest") and not row.get("human_ruling")
                ):
                    self.repository.record_human_review(
                        context_id, task_id, claim, str(row["request_id"]), None, None, None
                    )
                self.repository.update_task(task_id, context_id, claim, held_reason=reason)
                logger.error(
                    "policy_tool_call_retryable_failure", extra={**fields, "policy_reason_code": reason}
                )
                return False
        return True

    async def _reconcile_confirmations(
        self,
        context_id: str,
        task: dict[str, Any],
        claim: str,
        session_id: str,
        events: list[dict[str, Any]],
    ) -> bool:
        confirmations = {
            (str(e.get("tool_use_id")), str(e.get("result")))
            for e in events
            if e.get("type") == "user.tool_confirmation"
        }
        by_id = {str(event.get("id")): event for event in events}
        order = {event_id: index for index, event_id in enumerate(by_id)}
        requests = self.repository.list_input_requests(str(task["task_id"]))
        requests.sort(key=lambda row: order.get(str(row["cma_event_id"]), len(order)))
        for request in requests:
            if request["status"] != "resolving":
                continue
            event_id = str(request["cma_event_id"])
            source = by_id.get(event_id)
            if self.custom_tools.executions.get(str(task["task_id"]), event_id) is not None or (
                source and source.get("type") == "agent.custom_tool_use" and source.get("name") != "ask_user"
            ):
                try:
                    delivered = await self.custom_tools.reconcile(
                        context_id,
                        task,
                        claim,
                        session_id,
                        request,
                        source,
                        events,
                        self.policy_engine,
                        self._remaining(context_id),
                    )
                except DefinitiveProviderError:
                    self.repository.update_context(context_id, claim, lifecycle_state="failed")
                    self._finish(context_id, str(task["task_id"]), claim, "FAILED", "")
                    return True
                if delivered:
                    self._resolve_input(request, context_id, claim)
                    self.custom_tools.cleanup(str(task["task_id"]), event_id)
                    if self.snapshots is not None:
                        self.trigger.schedule(context_id, "policy-next-call")
                elif self.snapshots is not None:
                    return False
                continue
            if request["kind"] == "clarification":
                if any(
                    event.get("type") == "user.custom_tool_result"
                    and event.get("custom_tool_use_id") == event_id
                    for event in events
                ):
                    self._resolve_input(request, context_id, claim)
                    self.pending_input.delete(str(request["answer_object_key"]))
                    continue
                if request.get("response_attempted_at") is not None:
                    self.repository.update_task(
                        str(task["task_id"]), context_id, claim, held_reason="ambiguous-confirmation"
                    )
                    continue
                if not self.repository.mark_clarification_attempted(
                    str(request["request_id"]), context_id, claim
                ):
                    continue
                try:
                    answer_key = str(request["answer_object_key"])
                    answer = self.pending_input.get(answer_key)
                    await self.provider.send_custom_tool_result(
                        session_id, event_id, answer, is_error=bool(task.get("cancel_requested_at"))
                    )
                except DefinitiveProviderError:
                    self.repository.update_context(context_id, claim, lifecycle_state="failed")
                    self._finish(context_id, str(task["task_id"]), claim, "FAILED", "")
                    return True
                except Exception:
                    self.repository.update_task(
                        str(task["task_id"]), context_id, claim, held_reason="ambiguous-confirmation"
                    )
                    refreshed = await self.provider.list_events(session_id)
                    if not any(
                        event.get("type") == "user.custom_tool_result"
                        and event.get("custom_tool_use_id") == event_id
                        for event in refreshed
                    ):
                        continue
                self._resolve_input(request, context_id, claim)
                self.pending_input.delete(str(request["answer_object_key"]))
                continue
            decision = str(request["decision"])
            if (event_id, decision) in confirmations:
                self._resolve_input(request, context_id, claim)
                continue
            if request.get("confirmation_attempted_at") is not None:
                if task.get("held_reason") != "ambiguous-confirmation":
                    self.repository.update_task(
                        str(task["task_id"]), context_id, claim, held_reason="ambiguous-confirmation"
                    )
                logger.warning("cma_ambiguous_confirmation_held", extra={"request_id": request["request_id"]})
                continue
            if decision not in {"allow", "deny"}:
                self.repository.update_task(
                    str(task["task_id"]), context_id, claim, held_reason="policy-unpublished-decision"
                )
                continue
            if not self.repository.mark_confirmation_attempted(str(request["request_id"]), context_id, claim):
                continue
            try:
                reason_key = request.get("decision_reason_object_key")
                reason = (
                    self.pending_input.get(str(reason_key))
                    if reason_key
                    else (
                        "Denied by OpenAPPA policy."
                        if decision == "deny" and not task.get("cancel_requested_at")
                        else None
                    )
                )
                await self.provider.confirm_tool(session_id, event_id, decision == "allow", reason)
            except DefinitiveProviderError as error:
                self.repository.update_context(context_id, claim, lifecycle_state="failed")
                self._finish(context_id, str(task["task_id"]), claim, "FAILED", "")
                logger.error(
                    "cma_confirmation_failed",
                    extra={"request_id": request["request_id"], "error_type": type(error).__name__},
                )
                return True
            except Exception as error:
                self.repository.update_task(
                    str(task["task_id"]), context_id, claim, held_reason="ambiguous-confirmation"
                )
                logger.error(
                    "cma_ambiguous_confirmation",
                    extra={"request_id": request["request_id"], "error_type": type(error).__name__},
                )
                refreshed = await self.provider.list_events(session_id)
                if any(
                    event.get("type") == "user.tool_confirmation"
                    and event.get("tool_use_id") == event_id
                    and event.get("result") == decision
                    for event in refreshed
                ):
                    self._resolve_input(request, context_id, claim)
                continue
            self._resolve_input(request, context_id, claim)
        current_task = self.repository.get_task(str(task["task_id"])) or task
        reason = str(current_task.get("held_reason") or "")
        requests = self.repository.list_input_requests(str(task["task_id"]))
        if (
            (reason == "ambiguous-confirmation" and not any(row["status"] == "resolving" for row in requests))
            or (
                reason.startswith("policy-")
                and not any(
                    row["kind"] == "tool_approval" and row["status"] != "resolved" for row in requests
                )
            )
            or (reason.startswith("tool-") and not any(row["status"] != "resolved" for row in requests))
        ):
            self.repository.update_task(str(task["task_id"]), context_id, claim, held_reason=None)
        return False

    async def _reconcile_cancel(
        self,
        context_id: str,
        task: dict[str, Any],
        claim: str,
        session_id: str,
        events: list[dict[str, Any]],
    ) -> None:
        task_id = str(task["task_id"])
        relevant = _after(events, str(task["cma_input_event_id"]))
        status = _latest_status(relevant)
        pending_ids = set(((status or {}).get("stop_reason") or {}).get("event_ids", []))
        for source in relevant:
            if (
                source.get("id") in pending_ids
                and source.get("type") == "agent.custom_tool_use"
                and source.get("name") != "ask_user"
            ):
                event_id = str(source["id"])
                self.repository.add_input_request(
                    approval_request_id(task_id, event_id), task_id, event_id, context_id, claim
                )
                self.custom_tools.prepare(context_id, task_id, claim, source)
        interrupt_index = next((i for i, e in enumerate(relevant) if e.get("type") == "user.interrupt"), None)
        if interrupt_index is not None:
            idle = _latest_status(relevant[interrupt_index + 1 :])
            if (
                idle
                and str(idle.get("type")).replace("thread_status", "status")
                in {
                    "session.status_idle",
                    "session.status_terminated",
                }
                and (idle.get("stop_reason") or {}).get("type") != "requires_action"
            ):
                self._finish(context_id, task_id, claim, "CANCELED", str(idle.get("id", "")))
                return
            if idle and (idle.get("stop_reason") or {}).get("type") == "requires_action":
                for request in self.repository.list_input_requests(task_id):
                    if request["status"] == "pending" or (
                        request["kind"] == "tool_approval"
                        and request["status"] == "resolving"
                        and request.get("confirmation_attempted_at") is None
                    ):
                        if request["kind"] == "clarification":
                            key = str(uuid.uuid4())
                            self.pending_input.put(key, "Task cancelled by user.")
                            row = self.repository.decide_clarification_request(
                                str(request["request_id"]), task_id, key
                            )
                            if row is None or row["answer_object_key"] != key:
                                self.pending_input.delete(key)
                        else:
                            self.repository.deny_cancelled_input_request(
                                str(request["request_id"]), context_id, claim
                            )
                await self._reconcile_confirmations(context_id, task, claim, session_id, events)
                return
        if task.get("cancel_attempted_at"):
            if task.get("held_reason") != "ambiguous-interrupt":
                self.repository.update_task(task_id, context_id, claim, held_reason="ambiguous-interrupt")
            logger.warning("cma_ambiguous_interrupt_held", extra={"task_id": task_id})
            return
        self.repository.update_task(
            task_id, context_id, claim, cancel_attempted_at=datetime.now(UTC), internal_state="interrupting"
        )
        try:
            await self.provider.interrupt(session_id)
        except DefinitiveProviderError as error:
            self.repository.update_context(context_id, claim, lifecycle_state="failed")
            self._finish(context_id, task_id, claim, "FAILED", "")
            logger.error(
                "cma_interrupt_failed", extra={"task_id": task_id, "error_type": type(error).__name__}
            )
        except Exception as error:
            self.repository.update_task(task_id, context_id, claim, held_reason="ambiguous-interrupt")
            logger.error(
                "cma_ambiguous_interrupt", extra={"task_id": task_id, "error_type": type(error).__name__}
            )

    def _finish(self, context_id: str, task_id: str, claim: str, state: str, terminal_event_id: str) -> None:
        self.repository.update_task(
            task_id,
            context_id,
            claim,
            internal_state="terminal",
            a2a_state=state,
            cma_terminal_event_id=terminal_event_id or None,
            held_reason=None,
        )
        self.repository.update_context(context_id, claim, active_task_id=None)
