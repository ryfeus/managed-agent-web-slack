from __future__ import annotations

import inspect

import pytest

from managed_agents_app.cma_controller.repository import CmaControllerRepository
from managed_agents_app.db.thread_repository import ThreadRepository


def test_repositories_keep_application_and_controller_identity_separate() -> None:
    application_operations = (
        ThreadRepository.create_thread,
        ThreadRepository.bind_surface,
        ThreadRepository.record_logical_send,
        ThreadRepository.bind_task_id,
        ThreadRepository.claim_context_initialization,
        ThreadRepository.bind_context,
    )
    controller_operations = (
        CmaControllerRepository.create_initializing_context,
        CmaControllerRepository.bind_cma_session,
        CmaControllerRepository.create_task,
    )
    assert "thread_id" in inspect.signature(ThreadRepository.create_thread).parameters or any(
        "thread_id" in inspect.signature(operation).parameters for operation in application_operations
    )
    for operation in controller_operations:
        assert not {"thread_id", "principal_id", "surface", "slack_id"} & set(
            inspect.signature(operation).parameters
        )


def test_repository_rejects_invalid_identity_inputs_before_database_access(config) -> None:
    threads = ThreadRepository(config)
    controller = CmaControllerRepository(config)
    with pytest.raises(ValueError, match="agent_id"):
        threads.create_thread(config.dev_principal_id, "")
    with pytest.raises(ValueError, match="client_message_id"):
        threads.record_logical_send(config.dev_principal_id, "cma", "")
    with pytest.raises(ValueError, match="Lease duration"):
        threads.claim_context_initialization(config.dev_principal_id, 0)
    with pytest.raises(ValueError, match="context_id"):
        threads.bind_context(config.dev_principal_id, "claim", "")
    with pytest.raises(ValueError, match="Context and creation"):
        controller.create_initializing_context("", "message")
    with pytest.raises(ValueError, match="sequence"):
        controller.create_task("task", "context", "message", 0, "TASK_STATE_SUBMITTED", "queued")
