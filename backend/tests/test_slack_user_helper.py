"""Pure checks for the local-only real Slack user helper."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/slack_user.py"
SPEC = importlib.util.spec_from_file_location("slack_user_helper", SCRIPT)
assert SPEC and SPEC.loader
slack_user = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = slack_user
SPEC.loader.exec_module(slack_user)


def message(ts, user, text, root="1.0", bot_id=None):
    return slack_user.normalize_message(
        {"ts": ts, "user": user, "bot_id": bot_id, "text": text, "thread_ts": root}, root
    )


def state():
    return {
        "binding": {"thread_id": "thread-1", "context_id": "context-1", "agent_id": "cma"},
        "legacy_tables_present": [],
        "tasks": [
            {
                "task_id": f"task-{index}",
                "client_message_id": f"slack:event:T1:Ev{index}",
                "controller_state": "COMPLETED",
                "projection_status": "completed",
                "task_state": "COMPLETED",
                "push_receipts": index,
            }
            for index in (1, 2)
        ],
    }


def test_api_requires_ok_and_never_prints_token(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test-secret")
    response = httpx.Response(200, json={"ok": False, "error": "bad xoxp-test-secret"})
    client = Mock(post=Mock(return_value=response))
    with pytest.raises(slack_user.SlackUserError, match=r"bad \[REDACTED\]") as error:
        slack_user.SlackApi(client).call("xoxp-test-secret", "auth.test")
    assert "xoxp-test-secret" not in str(error.value)
    assert client.post.call_args.kwargs["headers"]["Authorization"] == "Bearer xoxp-test-secret"


def test_api_transport_error_is_secret_safe():
    client = Mock(post=Mock(side_effect=httpx.ConnectError("request included xoxp-secret")))
    with pytest.raises(slack_user.SlackUserError, match="transport error: ConnectError") as error:
        slack_user.SlackApi(client).call("xoxp-secret", "auth.test")
    assert "xoxp-secret" not in str(error.value)


def test_thread_normalizes_blocks_and_paginates():
    responses = [
        httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [{"ts": "1.0", "user": "U1", "text": "root"}],
                "has_more": True,
                "response_metadata": {"next_cursor": "next"},
            },
        ),
        httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"ts": "1.1", "user": "U2", "blocks": [{"type": "markdown_text", "text": "ACK_test"}]}
                ],
                "has_more": False,
            },
        ),
    ]
    client = Mock(post=Mock(side_effect=responses))
    result = slack_user.SlackApi(client).thread("token", "C1", "1.0")
    assert result["messages"][1]["content"] == "ACK_test"
    assert result["messages"][1]["thread_ts"] == "1.0"
    assert client.post.call_args.kwargs["data"]["cursor"] == "next"


def test_marker_requires_bot_identity_and_exactly_one_visible_occurrence():
    messages = [
        message("1.0", "U1", "<@U2> ACK_test"),
        message("1.1", "U1", "ACK_test"),
        message("1.2", "U3", "ACK_test"),
        message("1.3", "U2", "ACK_test"),
    ]
    matches = slack_user.marker_matches(messages, "ACK_test", "1.0", "U1", "U2", None)
    assert [item["ts"] for item in matches] == ["1.3"]
    messages.append(message("1.4", "U2", "ACK_test"))
    with pytest.raises(slack_user.SmokeFailure, match="More than one"):
        slack_user.marker_matches(messages, "ACK_test", "1.0", "U1", "U2", None)
    messages[-1]["content"] = "ACK_test ACK_test"
    with pytest.raises(slack_user.SmokeFailure, match="repeats"):
        slack_user.marker_matches(messages, "ACK_test", "1.0", "U1", "U2", None)


def test_marker_accepts_known_bot_id_and_block_text():
    candidate = message("1.1", None, "")
    candidate.update(bot_id="B2", block_text="ACK_test", content="ACK_test")
    assert slack_user.marker_matches([candidate], "ACK_test", "1.0", "U1", "U2", "B2") == [candidate]


def test_streamed_markdown_text_is_normalized():
    candidate = slack_user.normalize_message(
        {"ts": "1.1", "user": "U2", "text": "Working", "markdown_text": "ACK_test"}, "1.0"
    )
    assert slack_user.marker_matches([candidate], "ACK_test", "1.0", "U1", "U2", None) == [candidate]


@pytest.mark.parametrize(
    "blocks",
    [
        [{"type": "context_actions", "elements": []}],
        [{"type": "context", "elements": [{"type": "mrkdwn", "text": "Original message"}]}],
    ],
)
def test_notification_text_cannot_satisfy_visible_response_marker(blocks):
    candidate = slack_user.normalize_message(
        {"ts": "1.1", "user": "U2", "text": "ACK_test", "blocks": blocks}, "1.0"
    )
    assert "ACK_test" not in candidate["content"]
    assert slack_user.marker_matches([candidate], "ACK_test", "1.0", "U1", "U2", None) == []


def test_visible_blocks_win_over_notification_fallback_without_double_counting():
    candidate = slack_user.normalize_message(
        {
            "ts": "1.1",
            "user": "U2",
            "text": "ACK_test",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "ACK_test"}},
            ],
        },
        "1.0",
    )
    assert candidate["content"] == "ACK_test"
    assert slack_user.marker_matches([candidate], "ACK_test", "1.0", "U1", "U2", None) == [candidate]


def test_inspector_accepts_two_completed_slack_tasks_and_classifies_failures():
    result = state()
    assert slack_user.validate_inspection(result, "T1") is result
    result["tasks"][1]["controller_state"] = "WORKING"
    with pytest.raises(slack_user.SmokeFailure) as error:
        slack_user.validate_inspection(result, "T1")
    assert error.value.boundary == "agent_execution"
    result["tasks"][1]["controller_state"] = "COMPLETED"
    result["tasks"][1]["projection_status"] = "pending"
    with pytest.raises(slack_user.SmokeFailure) as error:
        slack_user.validate_inspection(result, "T1")
    assert error.value.boundary == "slack_projection"
    result["tasks"][1]["client_message_id"] = "web:agui:other"
    with pytest.raises(slack_user.SmokeFailure) as error:
        slack_user.validate_inspection(result, "T1")
    assert error.value.boundary == "a2a_task_creation"


def test_inspector_rejects_legacy_table_or_missing_catalog_result():
    result = state()
    result["legacy_tables_present"] = ["agent_sessions"]
    with pytest.raises(slack_user.SmokeFailure, match="Legacy application-session tables"):
        slack_user.validate_inspection(result, "T1")
    del result["legacy_tables_present"]
    with pytest.raises(slack_user.SmokeFailure, match="Legacy application-session tables"):
        slack_user.validate_inspection(result, "T1")


def test_poll_timeout_reports_first_observable_boundary(monkeypatch):
    api = Mock(thread=Mock(return_value={"messages": []}))
    monkeypatch.setattr(slack_user, "inspect_binding", lambda *_: {"binding": None, "tasks": []})
    with pytest.raises(slack_user.SmokeFailure) as error:
        slack_user.wait_for_marker(
            api,
            "token",
            "C1",
            "1.0",
            "ACK_test",
            "U1",
            "U2",
            None,
            "T1",
            1,
            timeout=0,
            clock=lambda: 5,
        )
    assert error.value.boundary == "slack_delivery"
    assert slack_user.timeout_boundary({"binding": {"thread_id": "thread-1"}, "tasks": []}, 2) == (
        "continuation_binding"
    )


def test_smoke_posts_real_user_turns_and_reuses_inspector(monkeypatch):
    for name, value in {
        "SLACK_USER_TOKEN": "xoxp-local",
        "SLACK_BOT_TOKEN": "xoxb-local",
        "E2E_LIVE_SLACK_CHANNEL_ID": "C1",
        "E2E_LIVE_SLACK_TEAM_ID": "T1",
        "DSQL_ENDPOINT": "example.dsql.amazonaws.com",
    }.items():
        monkeypatch.setenv(name, value)
    posted = []

    def post(token, channel, text, thread_ts=None):
        posted.append((token, channel, text, thread_ts))
        return {"channel": channel, "ts": "1.1" if thread_ts else "1.0", "thread_ts": thread_ts or "1.0"}

    def thread(_token, _channel, _root):
        first = posted[0][2].split()[-1]
        messages = [message("1.0", "U1", posted[0][2]), message("1.2", "U2", first)]
        if len(posted) == 2:
            second = posted[1][2].split()[-1]
            messages.extend([message("1.1", "U1", posted[1][2]), message("1.3", "U2", second)])
        return {"messages": messages}

    api = Mock(
        auth=Mock(side_effect=[{"team_id": "T1", "user_id": "U1"}, {"team_id": "T1", "user_id": "U2"}]),
        post=Mock(side_effect=post),
        thread=Mock(side_effect=thread),
    )
    monkeypatch.setattr(slack_user, "wait_for_inspection", lambda *_: state())
    report = slack_user.smoke(api)
    assert posted[0][0] == posted[1][0] == "xoxp-local"
    assert posted[0][3] is None and posted[1][3] == "1.0"
    assert report["task_ids"] == ["task-1", "task-2"]
    assert json.dumps(report).find("xoxp-local") == -1


def test_smoke_failure_preserves_identifiers_without_token(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("E2E_LIVE_SLACK_CHANNEL_ID", "C1")
    monkeypatch.setenv("DSQL_ENDPOINT", "dsql.example")
    monkeypatch.delenv("E2E_LIVE_SLACK_TEAM_ID", raising=False)
    api = Mock(auth=Mock(side_effect=slack_user.SlackUserError("bad xoxp-secret")))
    with pytest.raises(slack_user.SmokeFailure) as error:
        slack_user.smoke(api)
    assert error.value.boundary == "user_auth"
    assert "xoxp-secret" not in str(error.value)
    assert "run_id" in str(error.value)
