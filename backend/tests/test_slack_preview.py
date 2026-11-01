import json
from unittest.mock import MagicMock

from managed_agents_app.slack.preview import SlackPreviewHint, SqsSlackPreviewTrigger


def test_hint_contains_only_identifiers_and_deduplicates_per_card():
    client = MagicMock()
    trigger = SqsSlackPreviewTrigger("queue", client)
    trigger.start(SlackPreviewHint("binding", "task", "100.01"))
    trigger.start(SlackPreviewHint("binding", "task", "100.01"))
    trigger.start(SlackPreviewHint("binding", "task", "100.02"))
    first, retry, other = [call.kwargs for call in client.send_message.call_args_list]
    assert first["MessageGroupId"] == "binding"
    assert first["MessageDeduplicationId"] == retry["MessageDeduplicationId"]
    assert first["MessageDeduplicationId"] != other["MessageDeduplicationId"]
    assert set(json.loads(first["MessageBody"])) == {"v", "bindingId", "taskId", "createdAt"}
