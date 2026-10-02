"""Controller-only held-task inspection and manually verified resolution."""

from __future__ import annotations

import argparse
import json
from typing import Any

from managed_agents_app.cma_controller.composition import build_controller
from managed_agents_app.cma_controller.push_repository import PushRepository
from managed_agents_app.cma_controller.push_trigger import PushTrigger
from managed_agents_app.cma_controller.runtime_repository import ControllerRuntimeRepository
from managed_agents_app.cma_controller.trigger import SchedulerTrigger
from managed_agents_app.config import load_config


def held_task_summary(row: dict[str, Any]) -> dict[str, str | int]:
    return {
        "taskId": str(row["task_id"]),
        "contextId": str(row["context_id"]),
        "state": str(row["a2a_state"]),
        "internalState": str(row["internal_state"]),
        "reason": str(row["held_reason"]),
        "dispatchAttempts": int(row["dispatch_attempts"] or 0),
    }


def push_summary(row: dict[str, Any]) -> dict[str, str | int | None]:
    return {
        "taskId": str(row["task_id"]),
        "configId": str(row["config_id"]),
        "attemptCount": int(row["attempt_count"] or 0),
        "lastAttemptAt": str(row["last_attempt_at"]) if row["last_attempt_at"] else None,
        "permanentFailureAt": (str(row["permanent_failure_at"]) if row["permanent_failure_at"] else None),
    }


class ControllerAdmin:
    def __init__(
        self,
        repository: ControllerRuntimeRepository,
        trigger: SchedulerTrigger,
        push_trigger: PushTrigger | None = None,
    ) -> None:
        self.repository = repository
        self.trigger = trigger
        self.push_trigger = push_trigger

    def list_held(self) -> list[dict[str, str | int]]:
        return [held_task_summary(row) for row in self.repository.list_held_tasks()]

    def list_failed_push(self) -> list[dict[str, str | int | None]]:
        return [push_summary(row) for row in PushRepository(self.repository).list_permanently_failed()]

    def inspect_push(self, task_id: str, config_id: str) -> dict[str, str | int | None]:
        row = PushRepository(self.repository).get(task_id, config_id)
        if row is None:
            raise ValueError("Unknown or deleted push config")
        return push_summary(row)

    def retry_push(self, task_id: str, config_id: str) -> None:
        if self.push_trigger is None:
            raise RuntimeError("Push queue is not configured")
        task = self.repository.get_task(task_id)
        if task is None:
            raise ValueError("Unknown task")
        if not PushRepository(self.repository).reset_permanent_failure(task_id, config_id):
            raise RuntimeError("Push config is not permanently failed or has an active delivery claim")
        self.push_trigger.schedule_context(str(task["context_id"]), "admin-retry-push")

    def inspect(self, task_id: str) -> dict[str, str | int]:
        row = next((row for row in self.repository.list_held_tasks() if str(row["task_id"]) == task_id), None)
        if row is None:
            raise ValueError("Task is not held")
        return held_task_summary(row)

    def resolve(self, task_id: str, resolution: str, *, verified_no_side_effect: bool = False) -> None:
        if resolution == "retry" and not verified_no_side_effect:
            raise ValueError("Retry requires external verification that CMA did not accept the input")
        row = self.repository.get_task(task_id)
        if row is None:
            raise ValueError("Unknown task")
        context_id = str(row["context_id"])
        claim = self.repository.claim_scheduler(context_id)
        if claim is None:
            raise RuntimeError("Context scheduler lease is busy; retry this admin command")
        try:
            self.repository.resolve_held_task(task_id, claim, resolution)
        finally:
            self.repository.release_scheduler(context_id, claim)
        self.trigger.schedule(context_id, f"admin-{resolution}")
        if self.push_trigger is not None:
            self.push_trigger.schedule_context(context_id, f"admin-{resolution}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect and resolve held CMA A2A tasks")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("list-held")
    inspect = subcommands.add_parser("inspect")
    inspect.add_argument("task_id")
    fail = subcommands.add_parser("fail")
    fail.add_argument("task_id")
    retry = subcommands.add_parser("retry")
    retry.add_argument("task_id")
    retry.add_argument("--verified-no-side-effect", action="store_true", required=True)
    subcommands.add_parser("list-failed-push")
    inspect_push = subcommands.add_parser("inspect-push")
    inspect_push.add_argument("task_id")
    inspect_push.add_argument("config_id")
    retry_push = subcommands.add_parser("retry-push")
    retry_push.add_argument("task_id")
    retry_push.add_argument("config_id")
    args = parser.parse_args()
    config = load_config()
    composition = build_controller(config, with_dispatcher=False)
    repository = composition.repository
    admin = ControllerAdmin(repository, composition.scheduler_trigger, composition.push_trigger)
    if args.command in {"list-failed-push", "inspect-push"}:
        push_result = (
            admin.list_failed_push()
            if args.command == "list-failed-push"
            else admin.inspect_push(args.task_id, args.config_id)
        )
        print(json.dumps(push_result, sort_keys=True))
        return
    if args.command in {"list-held", "inspect"}:
        rows = repository.list_held_tasks()
        held_result = (
            [held_task_summary(row) for row in rows]
            if args.command == "list-held"
            else next(
                (held_task_summary(row) for row in rows if str(row["task_id"]) == args.task_id),
                None,
            )
        )
        if held_result is None:
            parser.error("Task is not held")
        print(json.dumps(held_result, sort_keys=True))
        return
    if args.command == "retry-push":
        if not config.cma_push_queue_url:
            parser.error("CMA_PUSH_QUEUE_URL is required to retry push")
        admin.retry_push(args.task_id, args.config_id)
        print(json.dumps({"taskId": args.task_id, "configId": args.config_id, "retried": True}))
        return
    if not config.cma_scheduler_queue_url:
        parser.error("CMA_SCHEDULER_QUEUE_URL is required for admin resolutions")
    admin.resolve(
        args.task_id,
        args.command,
        verified_no_side_effect=bool(getattr(args, "verified_no_side_effect", False)),
    )
    print(json.dumps({"taskId": args.task_id, "resolution": args.command}, sort_keys=True))


if __name__ == "__main__":
    main()
