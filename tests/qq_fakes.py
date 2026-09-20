"""QQBot 测试用的假实现。

和 :mod:`tests.fakes` 一个立场：**测试绝不联网**。
扫码登录、access_token、发消息全部通过注入假 transport / 假协议完成，
所以这些测试可以在没有网络、没有 QQ 账号的机器上跑。
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yuque_agent.qqbot.client import Target
from yuque_agent.qqbot.protocol import BindResult, BindStatus, BindTask, QQBotError

#: 固定的 32 字节绑定 key（真实场景是每次随机生成）。
BIND_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")


# ---------------------------------------------------------------- 传输层


@dataclass
class FakeResponse:
    """只实现 :class:`~yuque_agent.qqbot.protocol.HttpResponse` 需要的部分。"""

    payload: Any = None
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    raw_text: str | None = None

    @property
    def text(self) -> str:
        if self.raw_text is not None:
            return self.raw_text
        return json.dumps(self.payload, ensure_ascii=False)

    def json(self) -> Any:
        if self.raw_text is not None:
            return json.loads(self.raw_text)
        return self.payload


class FakeTransport:
    """按顺序吐响应的假 HTTP 传输；也支持用 ``handler`` 动态决定。"""

    def __init__(self, responses: list[Any] | None = None, handler: Any = None) -> None:
        self.responses = list(responses or [])
        self.handler = handler
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> FakeResponse:
        record = {
            "method": method.upper(),
            "url": url,
            "json": json,
            "headers": dict(headers or {}),
            "timeout": timeout,
        }
        self.requests.append(record)
        if self.handler is not None:
            return self.handler(record)
        if not self.responses:
            raise AssertionError(f"FakeTransport 没有排队的响应：{method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, FakeResponse) else FakeResponse(item)

    def close(self) -> None:
        self.closed = True

    # -- 断言小工具 -------------------------------------------------------
    def last(self) -> dict[str, Any]:
        assert self.requests, "还没有任何请求"
        return self.requests[-1]

    def urls(self) -> list[str]:
        return [item["url"] for item in self.requests]


def retcode(payload: dict[str, Any], retcode_value: int = 0, msg: str = "") -> FakeResponse:
    """拼一个 ``{"retcode":0,"data":…}`` 形状的响应。"""
    body: dict[str, Any] = {"retcode": retcode_value, "data": payload}
    if msg:
        body["msg"] = msg
    return FakeResponse(body)


# ---------------------------------------------------------------- 加密


def encrypt_secret(plain: str, key_base64: str = BIND_KEY) -> str:
    """用 AES-256-GCM 造一个 ``bot_encrypt_secret``（IV(12) + 密文 + Tag(16)）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = base64.b64decode(key_base64)
    iv = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(iv, plain.encode("utf-8"), None)
    return base64.b64encode(iv + ciphertext).decode("ascii")


# ---------------------------------------------------------------- 协议


class FakeProtocol:
    """假协议：按脚本吐 ``poll_bind_result``，用来测登录流程的各种分支。"""

    def __init__(
        self,
        *,
        polls: list[Any] | None = None,
        create_errors: int = 0,
        default_status: int = int(BindStatus.PENDING),
    ) -> None:
        self.polls = list(polls or [])
        self.create_errors = create_errors
        self.default_status = default_status
        self.created: list[str] = []
        self.poll_count = 0
        self.closed = False

    def create_bind_task(self) -> BindTask:
        if self.create_errors > 0:
            self.create_errors -= 1
            raise QQBotError("模拟 create_bind_task 失败")
        task_id = f"task-{len(self.created)}"
        self.created.append(task_id)
        return BindTask(task_id=task_id, key=BIND_KEY)

    def poll_bind_result(self, task_id: str) -> BindResult:
        self.poll_count += 1
        if not self.polls:
            return BindResult(status=self.default_status)
        item = self.polls.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def completed(
    app_id: str = "102000001", secret: str = "s3cr3t-value", openid: str = "u-1"
) -> BindResult:
    return BindResult(
        status=int(BindStatus.COMPLETED),
        bot_app_id=app_id,
        bot_encrypt_secret=encrypt_secret(secret),
        user_openid=openid,
    )


def pending() -> BindResult:
    return BindResult(status=int(BindStatus.PENDING))


def expired() -> BindResult:
    return BindResult(status=int(BindStatus.EXPIRED))


# ---------------------------------------------------------------- 发送 / 运行


class RecordingSender:
    """记录发出去的消息；可以让第 N 条失败。"""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.sent: list[tuple[Target, str]] = []
        self.fail_times = fail_times
        self.calls = 0

    def send_text(self, target: Target, content: str, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise QQBotError("模拟发送失败")
        self.sent.append((target, content))
        return {"id": f"m-{self.calls}"}

    def get_access_token(self, **_kwargs: Any) -> str:
        return "fake-token"

    def close(self) -> None:
        return None


@dataclass
class FakeRunResult:
    kind: str = "polling"
    verdict: str = "nothing_to_do"
    summary: str = "无事可做"
    run_id: str = "20260920-101834-polling-abcd"
    steps: int = 1
    tool_calls: int = 0
    emitted: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "verdict": self.verdict,
            "summary": self.summary,
            "run_id": self.run_id,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "emitted": self.emitted,
            "error": self.error,
        }


class FakeRunner:
    """按脚本返回 run 结果；``poll_results`` 里的 ``None`` 表示「没变化」。"""

    def __init__(
        self, *, poll_results: list[Any] | None = None, archive_result: Any = None
    ) -> None:
        self.poll_results = list(poll_results or [])
        self.archive_result = archive_result
        self.polls = 0
        self.archives = 0
        self.force_flags: list[bool] = []
        self.debounce_flags: list[bool] = []

    def poll_once(
        self, *, force: bool = False, rescan: bool = False, now: Any = None, debounce: bool = True
    ):
        self.polls += 1
        self.force_flags.append(force)
        self.debounce_flags.append(debounce)
        if self.poll_results:
            return self.poll_results.pop(0)
        return None

    def archive_once(self, *, now: Any = None):
        self.archives += 1
        return (
            self.archive_result
            if self.archive_result is not None
            else FakeRunResult(kind="archive")
        )


class FakeWatcher:
    """假的 Watcher：``tick`` 只记数。"""

    def __init__(self) -> None:
        self.ticks = 0
        self.last_result: Any = None
        self.last_kind = ""

    def tick(self, now: Any = None) -> list[str]:
        self.ticks += 1
        return []


# ---------------------------------------------------------------- 通知文件


def make_notice(
    path: Path,
    *,
    seq: int = 1,
    kind: str = "rejected",
    member: str = "张三",
    message: str = "「社团例会」这份申请我没法提交：时间写反了。",
    summary: str = "时间写反了",
    notice_id: str = "9f2c1a0b",
) -> Path:
    """在给定目录下写一条通知事件（形状照 ``docs/handoff.md`` §3.2）。"""
    payload = {
        "schema_version": "1.0",
        "seq": seq,
        "notice_id": notice_id,
        "created_at": "2026-09-20T10:19:24+08:00",
        "kind": kind,
        "repo": "g/kb",
        "doc": {"doc_id": 285808143, "title": "社团例会", "url": "https://nova.yuque.com/x"},
        "member": {"name": member},
        "summary": summary,
        "message": message,
        "reasons": [],
        "warnings": [],
        "extra": {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
