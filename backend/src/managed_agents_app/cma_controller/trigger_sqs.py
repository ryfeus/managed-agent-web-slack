"""AWS composition edge for controller-only scheduler wakeups."""

import boto3

from managed_agents_app.cma_controller.push_trigger import SqsPushTrigger
from managed_agents_app.cma_controller.trigger import SqsSchedulerTrigger


def production_trigger(queue_url: str) -> SqsSchedulerTrigger:
    return SqsSchedulerTrigger(queue_url, boto3.client("sqs"))


def production_push_trigger(queue_url: str) -> SqsPushTrigger:
    return SqsPushTrigger(queue_url, boto3.client("sqs"))
