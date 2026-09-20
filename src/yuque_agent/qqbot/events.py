"""入站事件归一化：把 QQ 网关/Webhook 的原始事件变成 :class:`InboundMessage`。

纯函数、无网络、无 asyncio——方便离线测试，也让 ``gateway.py`` 只剩下「连上去、收帧」。

事件形状与参考实现 ``qqbot_backend_sdk/gateway/event_dispatcher.py`` 一致：

* ``C2C_MESSAGE_CREATE``：``author.user_openid`` / ``content`` / ``id``
* ``GROUP_AT_MESSAGE_CREATE``：``author.member_openid`` / ``group_openid`` / ``content``
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .client import Target

EVENT_READY = "READY"
EVENT_RESUMED = "RESUMED"
EVENT_C2C_MESSAGE_CREATE = "C2C_MESSAGE_CREATE"
EVENT_GROUP_AT_MESSAGE_CREATE = "GROUP_AT_MESSAGE_CREATE"
EVENT_GROUP_MESSAGE_CREATE = "GROUP_MESSAGE_CREATE"
EVENT_AT_MESSAGE_CREATE = "AT_MESSAGE_CREATE"
EVENT_DIRECT_MESSAGE_CREATE = "DIRECT_MESSAGE_CREATE"

MESSAGE_EVENTS = (
    EVENT_C2C_MESSAGE_CREATE,
    EVENT_GROUP_AT_MESSAGE_CREATE,
    EVENT_GROUP_MESSAGE_CREATE,
    EVENT_AT_MESSAGE_CREATE,
    EVENT_DIRECT_MESSAGE_CREATE,
)

#: 群消息里 @机器人 会留下 ``<@!123456>`` 这类痕迹，解析命令前先摘掉。
_MENTION_RE = re.compile(r"<@!?\d+>|@\S+\s*")


@dataclass
class InboundMessage:
    """一条归一化后的入站消息（屏蔽 C2C / 群 / 频道的差异）。"""

    kind: str
    """``c2c`` | ``group`` | ``guild`` | ``dm`` | ``unknown``。"""

    sender_id: str
    content: str
    message_id: str = ""
    group_openid: str = ""
    sender_name: str = ""
    timestamp: str = ""
    event_type: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    raw: Any = field(default=None, repr=False)

    @property
    def text(self) -> str:
        """去掉 @机器人 的痕迹并 strip 之后的正文。"""
        return _MENTION_RE.sub("", self.content or "").strip()

    @property
    def reply_target(self) -> Target | None:
        """回哪去。没有 ``message_id`` 就不能被动回复，直接返回 ``None``。"""
        if self.kind == "c2c" and self.sender_id and self.message_id:
            return Target("c2c", self.sender_id, self.message_id)
        if self.kind == "group" and self.group_openid and self.message_id:
            return Target("group", self.group_openid, self.message_id)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "senderId": self.sender_id,
            "senderName": self.sender_name,
            "content": self.content,
            "messageId": self.message_id,
            "groupOpenid": self.group_openid,
            "timestamp": self.timestamp,
            "eventType": self.event_type,
        }


def parse_event(event_type: str, data: Any) -> InboundMessage | None:
    """把网关/Webhook 的一条事件转成 :class:`InboundMessage`；不是消息事件则返回 ``None``。"""
    if not isinstance(data, dict):
        return None
    author = data.get("author") if isinstance(data.get("author"), dict) else {}

    if event_type == EVENT_C2C_MESSAGE_CREATE:
        return InboundMessage(
            kind="c2c",
            sender_id=str(author.get("user_openid") or author.get("openid") or ""),
            content=str(data.get("content") or ""),
            message_id=str(data.get("id") or ""),
            sender_name=str(author.get("username") or ""),
            timestamp=str(data.get("timestamp") or ""),
            event_type=event_type,
            attachments=_attachments(data),
            raw=data,
        )

    if event_type in (EVENT_GROUP_AT_MESSAGE_CREATE, EVENT_GROUP_MESSAGE_CREATE):
        return InboundMessage(
            kind="group",
            sender_id=str(author.get("member_openid") or author.get("user_openid") or ""),
            content=str(data.get("content") or ""),
            message_id=str(data.get("id") or ""),
            group_openid=str(data.get("group_openid") or ""),
            sender_name=str(author.get("username") or ""),
            timestamp=str(data.get("timestamp") or ""),
            event_type=event_type,
            attachments=_attachments(data),
            raw=data,
        )

    if event_type == EVENT_AT_MESSAGE_CREATE:
        return InboundMessage(
            kind="guild",
            sender_id=str(author.get("id") or ""),
            content=str(data.get("content") or ""),
            message_id=str(data.get("id") or ""),
            sender_name=str(author.get("username") or ""),
            timestamp=str(data.get("timestamp") or ""),
            event_type=event_type,
            attachments=_attachments(data),
            raw=data,
        )

    if event_type == EVENT_DIRECT_MESSAGE_CREATE:
        return InboundMessage(
            kind="dm",
            sender_id=str(author.get("id") or ""),
            content=str(data.get("content") or ""),
            message_id=str(data.get("id") or ""),
            sender_name=str(author.get("username") or ""),
            timestamp=str(data.get("timestamp") or ""),
            event_type=event_type,
            attachments=_attachments(data),
            raw=data,
        )

    return None


def _attachments(data: dict[str, Any]) -> list[dict[str, Any]]:
    items = data.get("attachments")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


__all__ = [
    "EVENT_AT_MESSAGE_CREATE",
    "EVENT_C2C_MESSAGE_CREATE",
    "EVENT_DIRECT_MESSAGE_CREATE",
    "EVENT_GROUP_AT_MESSAGE_CREATE",
    "EVENT_GROUP_MESSAGE_CREATE",
    "EVENT_READY",
    "EVENT_RESUMED",
    "MESSAGE_EVENTS",
    "InboundMessage",
    "parse_event",
]
