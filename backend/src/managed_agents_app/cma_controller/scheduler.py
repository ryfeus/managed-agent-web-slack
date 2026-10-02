"""One bounded, fenced reconciliation step for a CMA context."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from managed_agents_app.cma_controller.pending_input import PendingInputStore
from managed_agents_app.cma_controller.provider import CmaProvider, DefinitiveProviderError
from managed_agents_app.cma_controller.push_trigger import PushTrigger
from managed_agents_app.cma_controller.runtime_repository import ControllerRuntimeRepository
from managed_agents_app.cma_controller.trigger import SchedulerTrigger

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.pending_input = pending_input
        self.trigger = trigger
        self.push_trigger = push_trigger

    def sweep_orphans(self) -> tuple[int, int]:
        referenced = self.repository.pending_input_keys()
        deleted = 0
        for key in self.pending_input.list_old_keys(86400):
            if key not in referenced:
                self.pending_input.delete(key)
                deleted += 1
        stale = self.repository.stale_pending_input_count()
        if stale:
            logger.warning("cma_stale_pending_input", extra={"count": stale})
        return deleted, stale

    async def run_once(self, context_id: str) -> None:
        claim = self.repository.claim_scheduler(context_id)
        if claim is None:
            raise SchedulerBusy(context_id)
        try:
            await self._reconcile(context_id, claim)
        finally:
            self.repository.release_scheduler(context_id, claim)
            if self.push_trigger is not None:
                try:
                    self.push_trigger.schedule_context(context_id, "scheduler-reconciled")
                except Exception:
                    logger.exception("cma_push_wakeup_failed", extra={"context_id": context_id})

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
            self.repository.update_task(
                task_id, context_id, claim, internal_state="input_required", a2a_state="INPUT_REQUIRED"
            )
            return
        if kind in {"session.status_terminated", "session.budget_reached"}:
            self.repository.update_context(context_id, claim, lifecycle_state="failed")
            self._finish(context_id, task_id, claim, "FAILED", str(status.get("id", "")))
        elif kind == "session.status_idle":
            stop_reason = status.get("stop_reason") or {}
            if stop_reason.get("type") == "requires_action":
                ids = {str(item) for item in stop_reason.get("event_ids", [])}
                by_id = {str(event.get("id")): event for event in events}
                for event_id in ids:
                    source = by_id.get(event_id)
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
                self.repository.update_task(
                    task_id, context_id, claim, internal_state="input_required", a2a_state="INPUT_REQUIRED"
                )
            elif stop_reason.get("type") in {"end_turn", "budget_reached"}:
                state = "FAILED" if stop_reason.get("type") == "budget_reached" else "COMPLETED"
                if state == "FAILED":
                    self.repository.update_context(context_id, claim, lifecycle_state="failed")
                self._finish(context_id, task_id, claim, state, str(status.get("id", "")))
        elif kind == "session.status_running":
            self.repository.update_task(
                task_id, context_id, claim, internal_state="waiting", a2a_state="WORKING"
            )

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
        for request in self.repository.list_input_requests(str(task["task_id"])):
            if request["status"] != "resolving":
                continue
            event_id = str(request["cma_event_id"])
            if request["kind"] == "clarification":
                if any(
                    event.get("type") == "user.custom_tool_result"
                    and event.get("custom_tool_use_id") == event_id
                    for event in events
                ):
                    self.repository.resolve_input_request(str(request["request_id"]), context_id, claim)
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
                self.repository.resolve_input_request(str(request["request_id"]), context_id, claim)
                self.pending_input.delete(str(request["answer_object_key"]))
                continue
            decision = str(request["decision"])
            if (event_id, decision) in confirmations:
                self.repository.resolve_input_request(str(request["request_id"]), context_id, claim)
                continue
            if request.get("confirmation_attempted_at") is not None:
                if task.get("held_reason") != "ambiguous-confirmation":
                    self.repository.update_task(
                        str(task["task_id"]), context_id, claim, held_reason="ambiguous-confirmation"
                    )
                logger.warning("cma_ambiguous_confirmation_held", extra={"request_id": request["request_id"]})
                continue
            if not self.repository.mark_confirmation_attempted(str(request["request_id"]), context_id, claim):
                continue
            try:
                reason_key = request.get("decision_reason_object_key")
                reason = self.pending_input.get(str(reason_key)) if reason_key else None
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
                    self.repository.resolve_input_request(str(request["request_id"]), context_id, claim)
                continue
            self.repository.resolve_input_request(str(request["request_id"]), context_id, claim)
        current_task = self.repository.get_task(str(task["task_id"])) or task
        if current_task.get("held_reason") == "ambiguous-confirmation" and not any(
            row["status"] == "resolving" for row in self.repository.list_input_requests(str(task["task_id"]))
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
                    if request["status"] == "pending":
                        if request["kind"] == "clarification":
                            key = str(uuid.uuid4())
                            self.pending_input.put(key, "Task cancelled by user.")
                            row = self.repository.decide_clarification_request(
                                str(request["request_id"]), task_id, key
                            )
                            if row is None or row["answer_object_key"] != key:
                                self.pending_input.delete(key)
                        else:
                            self.repository.decide_input_request(str(request["request_id"]), task_id, "deny")
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
