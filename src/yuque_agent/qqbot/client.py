"""QQBot 消息客户端：access_token 缓存 + 发消息。

对应参考实现 ``qqbot_backend_sdk`` 的 ``client.py`` / ``auth/token.py`` / ``api/messages.py``，
但只保留本项目真正用得上的部分（发文本 / Markdown、取网关地址、REST 逃生舱）：

* access_token 按 AppID 缓存在内存里，**提前刷新**（剩余时间 < min(5min, 剩余/3) 就重取），
  避免每条通知都打一次 ``bots.qq.com``；
* 被动回复带 ``msg_id`` + ``msg_seq``；主动推送只有 ``content``（与开放平台要求一致）；
* C2C → ``/v2/users/<openid>/messages``，群 → ``/v2/groups/<group_openid>/messages``。
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .credentials import CredentialStore, QQBotAccount, resolve_account
from .protocol import (
    MSG_TYPE_MARKDOWN,
    MSG_TYPE_TEXT,
    SCOPE_C2C,
    SCOPE_GROUP,
    QQBotError,
    QQBotProtocol,
    TokenInfo,
)

#: 提前刷新阈值：最多提前 5 分钟。
REFRESH_AHEAD_SECONDS = 5 * 60


def next_msg_seq() -> int:
    """被动回复的消息序号（0..65535），同一 ``msg_id`` 下不能重复。"""
    time_part = int(time.time() * 1000) % 100_000_000
    return (time_part ^ random.randint(0, 65535)) % 65536


@dataclass(frozen=True)
class Target:
    """出站目标：``c2c``（私聊）或 ``group``（群聊）。

    ``msg_id`` 有值 = **被动回复**（在用户消息的回复窗口内，不需要额外权限）；
    没有 = **主动推送**（QQ 对主动推送有严格配额，会失败是正常现象）。
    """

    scope: str
    target_id: str
    msg_id: str | None = None

    def __post_init__(self) -> None:
        if self.scope not in (SCOPE_C2C, SCOPE_GROUP):
            raise ValueError(f"scope 只能是 {SCOPE_C2C!r} 或 {SCOPE_GROUP!r}，收到 {self.scope!r}")
        if not self.target_id:
            raise ValueError("target_id 不能为空")

    @property
    def path(self) -> str:
        if self.scope == SCOPE_C2C:
            return f"/v2/users/{self.target_id}/messages"
        return f"/v2/groups/{self.target_id}/messages"

    def to_str(self) -> str:
        """``c2c:OPENID`` / ``group:GROUP_OPENID``（``msg_id`` 不参与序列化）。"""
        return f"{self.scope}:{self.target_id}"

    @classmethod
    def parse(cls, text: str, *, msg_id: str | None = None) -> Target:
        """解析 ``c2c:OPENID`` / ``group:GROUP_OPENID``；只给 ``OPENID`` 时默认按私聊。"""
        raw = (text or "").strip()
        if not raw:
            raise ValueError("目标不能为空，写法：c2c:<openid> 或 group:<group_openid>")
        scope, sep, target_id = raw.partition(":")
        if not sep:
            return cls(SCOPE_C2C, raw, msg_id=msg_id)
        scope = scope.strip().lower()
        aliases = {
            "user": SCOPE_C2C,
            "friend": SCOPE_C2C,
            "私聊": SCOPE_C2C,
            "群": SCOPE_GROUP,
            "群聊": SCOPE_GROUP,
        }
        scope = aliases.get(scope, scope)
        return cls(scope, target_id.strip(), msg_id=msg_id)


@runtime_checkable
class MessageSender(Protocol):
    """投递桥只依赖这一个方法，方便用假实现做离线测试。"""

    def send_text(self, target: Target, content: str, **kwargs: Any) -> dict[str, Any]: ...


class NullSender:
    """``--dry-run`` 用的假发送器：什么都不发，只记录。"""

    def __init__(self, log: Callable[[str], None] | None = None) -> None:
        self.log = log
        self.sent: list[tuple[Target, str]] = []

    def send_text(self, target: Target, content: str, **kwargs: Any) -> dict[str, Any]:
        self.sent.append((target, content))
        if self.log is not None:
            self.log(f"[dry-run] 本应发送给 {target.to_str()}：{content}")
        return {"dry_run": True, "target": target.to_str(), "chars": len(content)}

    def get_access_token(self, **_kwargs: Any) -> str:
        return "(dry-run)"

    def close(self) -> None:
        return None


class QQBotClient:
    """一个机器人账户的同步客户端。"""

    def __init__(
        self,
        account: QQBotAccount,
        *,
        protocol: QQBotProtocol | None = None,
        markdown_support: bool | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if not account.complete:
            raise QQBotError(
                "缺少 AppID / AppSecret：先跑 `yqa qq login` 扫码绑定，或设 YQA_QQ_APPID/YQA_QQ_SECRET"
            )
        self.account = account
        self.markdown_support = (
            account.markdown_support if markdown_support is None else markdown_support
        )
        self.log = log
        self._protocol = protocol or QQBotProtocol(env=account.env)
        self._owns_protocol = protocol is None
        self._tokens: dict[str, TokenInfo] = {}
        self._lock = threading.Lock()

    # -- 构造 -------------------------------------------------------------
    @classmethod
    def from_account(cls, account: QQBotAccount, **kwargs: Any) -> QQBotClient:
        return cls(account, **kwargs)

    @classmethod
    def from_store(
        cls,
        store: CredentialStore | None = None,
        *,
        account: str = "default",
        **kwargs: Any,
    ) -> QQBotClient:
        resolved = resolve_account(account=account, store=store)
        return cls(resolved, **kwargs)

    # -- 生命周期 ---------------------------------------------------------
    def close(self) -> None:
        if self._owns_protocol:
            self._protocol.close()

    def __enter__(self) -> QQBotClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def app_id(self) -> str:
        return self.account.app_id

    @property
    def protocol(self) -> QQBotProtocol:
        return self._protocol

    # -- 鉴权 -------------------------------------------------------------
    def get_access_token(self, *, force: bool = False) -> str:
        """取 access_token（带缓存与提前刷新）。"""
        app_id = self.account.app_id
        with self._lock:
            cached = self._tokens.get(app_id)
            if not force and cached is not None and _is_fresh(cached):
                return cached.access_token
        token = self._protocol.get_access_token(app_id, self.account.app_secret)
        with self._lock:
            self._tokens[app_id] = token
        return token.access_token

    def token_status(self) -> dict[str, Any]:
        with self._lock:
            cached = self._tokens.get(self.account.app_id)
        if cached is None:
            return {"status": "none", "expiresAt": None}
        remaining = cached.expires_at - time.time()
        return {
            "status": "valid" if _is_fresh(cached) else "stale",
            "expiresAt": cached.expires_at,
            "remainingSeconds": round(remaining, 1),
        }

    def clear_token_cache(self) -> None:
        with self._lock:
            self._tokens.clear()

    # -- REST -------------------------------------------------------------
    def api(self, method: str, path: str, body: Any = None) -> Any:
        """任意开放平台 REST 接口（逃生舱）。"""
        return self._protocol.api_request(self.get_access_token(), method, path, body)

    def gateway_url(self) -> str:
        """取 WebSocket 网关地址（``GET /gateway``）。"""
        data = self.api("GET", "/gateway")
        url = (data or {}).get("url") if isinstance(data, dict) else None
        if not url:
            raise QQBotError(f"网关接口没有返回 url：{data!r}")
        return str(url)

    # -- 发送 -------------------------------------------------------------
    def send_raw(self, target: Target, body: dict[str, Any]) -> dict[str, Any]:
        """按开放平台原样发一条消息（``msg_seq`` / ``msg_type`` 缺了就补）。"""
        payload = dict(body)
        if payload.get("msg_type") is None:
            if payload.get("markdown"):
                payload["msg_type"] = MSG_TYPE_MARKDOWN
            elif payload.get("media"):
                payload["msg_type"] = 7
            else:
                payload["msg_type"] = MSG_TYPE_TEXT
        if target.msg_id:
            payload.setdefault("msg_id", target.msg_id)
            payload.setdefault("msg_seq", next_msg_seq())
        cleaned = {key: value for key, value in payload.items() if value is not None}
        response = self._protocol.api_request(self.get_access_token(), "POST", target.path, cleaned)
        return response if isinstance(response, dict) else {"raw": response}

    def send_text(
        self,
        target: Target,
        content: str,
        *,
        markdown: bool | None = None,
        msg_seq: int | None = None,
        keyboard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发文本。``markdown=None`` 时跟随账户的 Markdown 权限。"""
        text = (content or "").strip()
        if not text:
            raise QQBotError("拒绝发送空消息")
        use_markdown = self.markdown_support if markdown is None else markdown
        body: dict[str, Any] = {}
        if use_markdown:
            body["markdown"] = {"content": text}
        else:
            body["content"] = text
        if keyboard:
            body["keyboard"] = keyboard
        if msg_seq is not None:
            body["msg_seq"] = msg_seq
        return self.send_raw(target, body)

    def send_markdown(
        self, target: Target, content: str, *, keyboard: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.send_text(target, content, markdown=True, keyboard=keyboard)

    def send_to(self, target: str | Target, content: str, **kwargs: Any) -> dict[str, Any]:
        """``send_to("group:xxx", "文本")`` 的便利写法。"""
        resolved = target if isinstance(target, Target) else Target.parse(target)
        return self.send_text(resolved, content, **kwargs)


def _is_fresh(token: TokenInfo) -> bool:
    """是否还能直接用：剩余时间要大于 刷新提前量。"""
    remaining = token.expires_at - time.time()
    refresh_ahead = min(REFRESH_AHEAD_SECONDS, max(remaining, 0.0) / 3)
    return remaining > refresh_ahead


__all__ = [
    "REFRESH_AHEAD_SECONDS",
    "MessageSender",
    "NullSender",
    "QQBotClient",
    "Target",
    "next_msg_seq",
]
