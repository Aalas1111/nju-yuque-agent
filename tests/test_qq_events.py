"""入站事件归一化测试。"""

from __future__ import annotations

from yuque_agent.qqbot.events import (
    EVENT_AT_MESSAGE_CREATE,
    EVENT_C2C_MESSAGE_CREATE,
    EVENT_GROUP_AT_MESSAGE_CREATE,
    InboundMessage,
    parse_event,
)


def test_parse_c2c_message() -> None:
    message = parse_event(
        EVENT_C2C_MESSAGE_CREATE,
        {
            "id": "msg-1",
            "content": "/status",
            "timestamp": "2026-09-20T10:00:00Z",
            "author": {"user_openid": "u-1"},
        },
    )
    assert message is not None
    assert message.kind == "c2c"
    assert message.sender_id == "u-1"
    assert message.message_id == "msg-1"
    target = message.reply_target
    assert target is not None and target.scope == "c2c" and target.msg_id == "msg-1"


def test_parse_group_message_uses_member_openid() -> None:
    message = parse_event(
        EVENT_GROUP_AT_MESSAGE_CREATE,
        {
            "id": "msg-2",
            "content": " /status",
            "group_openid": "g-1",
            "author": {"member_openid": "u-2", "username": "张三"},
        },
    )
    assert message is not None
    assert message.kind == "group"
    assert message.sender_id == "u-2"
    assert message.group_openid == "g-1"
    assert message.sender_name == "张三"
    assert message.text == "/status"
    target = message.reply_target
    assert target is not None and target.scope == "group" and target.target_id == "g-1"


def test_mentions_are_stripped_from_text() -> None:
    message = parse_event(
        EVENT_GROUP_AT_MESSAGE_CREATE,
        {
            "id": "m",
            "content": "<@!12345> /run",
            "group_openid": "g",
            "author": {"member_openid": "u"},
        },
    )
    assert message is not None
    assert message.text == "/run"


def test_guild_message_is_parsed_but_has_no_reply_target() -> None:
    message = parse_event(
        EVENT_AT_MESSAGE_CREATE,
        {"id": "m", "content": "hi", "channel_id": "c", "author": {"id": "u"}},
    )
    assert message is not None
    assert message.kind == "guild"
    assert message.reply_target is None  # 频道消息这条链路暂不支持回复


def test_unknown_event_is_ignored() -> None:
    assert parse_event("GUILD_CREATE", {"id": "g"}) is None
    assert parse_event("READY", {"session_id": "s"}) is None
    assert parse_event(EVENT_C2C_MESSAGE_CREATE, "not-a-dict") is None


def test_missing_message_id_means_no_reply_target() -> None:
    message = parse_event(
        EVENT_C2C_MESSAGE_CREATE, {"content": "hi", "author": {"user_openid": "u"}}
    )
    assert message is not None
    assert message.reply_target is None


def test_attachments_and_to_dict() -> None:
    message = parse_event(
        EVENT_C2C_MESSAGE_CREATE,
        {
            "id": "m",
            "content": "hi",
            "author": {"user_openid": "u"},
            "attachments": [{"url": "https://x/1.png"}, "junk"],
        },
    )
    assert message is not None
    assert len(message.attachments) == 1
    payload = message.to_dict()
    assert payload["kind"] == "c2c" and payload["senderId"] == "u"


def test_inbound_message_defaults_are_safe() -> None:
    message = InboundMessage(kind="c2c", sender_id="", content="")
    assert message.text == ""
    assert message.reply_target is None
