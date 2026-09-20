"""扫码登录：把「用户拿手机扫一下」这件事封装成**同步、可中断、可重放**的流程。

对应参考实现 ``qqbot_backend_sdk`` 的 ``auth/qr_connect.py`` + ``auth/qr_login.py``：

===============================  ==========================================
参考实现                          本模块
===============================  ==========================================
``start_qr_connect``（轮询循环）    :class:`QrLoginFlow`
``qr_connect``（等结果）            :meth:`QrLoginFlow.run`
``start/wait/cancel_qr_login``     :class:`QrLoginManager`（两阶段，可给后端/HTTP 用）
===============================  ==========================================

流程::

    create_bind_task → build_connect_url → 出示二维码
      → 每 poll_interval 秒 poll_bind_result
          status=PENDING   继续等
          status=COMPLETED 用本地 key 解密 AppSecret → 成功
          status=EXPIRED   二维码过期 → 重新 create_bind_task（刷新二维码）
      → 单张二维码超过 qr_timeout 也刷新
      → 取消 / 连续报错超过 max_errors / 刷新次数用尽 → 失败

**key 只存在内存里**（``BindTask.key``），二维码地址只是 ``task_id``，不含密钥。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .protocol import (
    BindTask,
    QQBotError,
    QQBotProtocol,
    build_connect_url,
    decrypt_secret,
)

DEFAULT_SOURCE = "yuque-agent"
DEFAULT_POLL_INTERVAL = 2.0
"""轮询间隔（秒）——与参考实现一致，2 秒。"""

DEFAULT_QR_TIMEOUT = 120.0
"""单张二维码等多久算超时（秒），超时就刷新一张新的。"""

DEFAULT_MAX_REFRESHES = 6
"""最多刷新几张二维码。6 张 ≈ 十来分钟，够人走到手机旁边了。"""

DEFAULT_MAX_ERRORS = 5
"""连续网络/接口报错多少次就放弃。"""

QrCallback = Callable[[str, int, str | None], None]
"""``on_qr(url, attempt, data_url)``——出示二维码时回调。``data_url`` 可能为 ``None``。"""

StatusCallback = Callable[[str], None]
"""``on_status(text)``——给用户看的进度文字。"""


@dataclass
class QrLoginResult:
    """一次扫码登录的最终结果。"""

    connected: bool
    message: str = ""
    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    user_openid: str = ""
    qr_url: str = ""
    qr_data_url: str = ""
    refreshes: int = 0
    """刷新了几次二维码（0 = 第一张就扫上了）。"""

    @property
    def credentials(self) -> list[dict[str, Any]]:
        """与参考实现 ``wait_qr_login`` 返回的 ``credentials`` 同形状。"""
        if not self.connected:
            return []
        return [
            {
                "appId": self.app_id,
                "appSecret": self.app_secret,
                "userOpenid": self.user_openid,
            }
        ]

    def to_dict(self, *, include_secret: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "connected": self.connected,
            "message": self.message,
            "appId": self.app_id,
            "userOpenid": self.user_openid,
            "qrUrl": self.qr_url,
            "refreshes": self.refreshes,
        }
        if include_secret and self.connected:
            payload["credentials"] = self.credentials
        return payload


class QrLoginFlow:
    """一次扫码登录（阻塞式）。所有外部依赖都可注入，所以能完全离线测。"""

    def __init__(
        self,
        protocol: QQBotProtocol,
        *,
        source: str = DEFAULT_SOURCE,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        qr_timeout: float = DEFAULT_QR_TIMEOUT,
        max_refreshes: int = DEFAULT_MAX_REFRESHES,
        max_errors: int = DEFAULT_MAX_ERRORS,
        retry_delay: float = 2.0,
        with_data_url: bool = False,
        cancel: threading.Event | None = None,
        on_qr: QrCallback | None = None,
        on_status: StatusCallback | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.protocol = protocol
        self.source = source
        self.poll_interval = poll_interval
        self.qr_timeout = qr_timeout
        self.max_refreshes = max_refreshes
        self.max_errors = max_errors
        self.retry_delay = retry_delay
        self.with_data_url = with_data_url
        self.cancel_event = cancel or threading.Event()
        self.on_qr = on_qr
        self.on_status = on_status
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self.qr_url = ""
        self.qr_data_url = ""

    # -- 对外 -------------------------------------------------------------
    def cancel(self) -> None:
        """请求中止（线程安全；正在 ``sleep`` 的那一步会在醒来后退出）。"""
        self.cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def run(self) -> QrLoginResult:
        errors = 0
        refreshes = 0
        while refreshes <= self.max_refreshes:
            if self.cancelled:
                return self._failure("已取消扫码登录")

            try:
                task = self.protocol.create_bind_task()
            except QQBotError as exc:
                errors += 1
                if errors > self.max_errors:
                    return self._failure(f"创建绑定任务失败（已重试 {self.max_errors} 次）：{exc}")
                self._status(f"创建绑定任务失败，{self.retry_delay:.0f} 秒后重试：{exc}")
                self._sleep(self.retry_delay)
                continue

            self._show(task, refreshes + 1)
            deadline = self._clock() + self.qr_timeout
            while self._clock() < deadline:
                if self.cancelled:
                    return self._failure("已取消扫码登录")
                self._sleep(self.poll_interval)
                if self.cancelled:
                    return self._failure("已取消扫码登录")
                try:
                    result = self.protocol.poll_bind_result(task.task_id)
                except QQBotError as exc:
                    errors += 1
                    if errors > self.max_errors:
                        return self._failure(
                            f"轮询绑定结果失败（已重试 {self.max_errors} 次）：{exc}"
                        )
                    self._status(f"轮询失败（第 {errors} 次），继续等：{exc}")
                    continue

                if result.completed:
                    try:
                        secret = decrypt_secret(result.bot_encrypt_secret, task.key)
                    except QQBotError as exc:
                        return self._failure(str(exc))
                    if not result.bot_app_id:
                        return self._failure("绑定成功，但开放平台没有返回 AppID")
                    return QrLoginResult(
                        connected=True,
                        message=f"绑定成功！AppID: {result.bot_app_id}",
                        app_id=result.bot_app_id,
                        app_secret=secret,
                        user_openid=result.user_openid,
                        qr_url=self.qr_url,
                        qr_data_url=self.qr_data_url,
                        refreshes=refreshes,
                    )

                if result.expired:
                    self._status("二维码已过期，正在刷新…")
                    break
                # PENDING / NONE：还没扫，继续等（不打印噪音日志）
            else:
                self._status("二维码超时，正在刷新…")

            refreshes += 1

        return self._failure(f"二维码刷新了 {self.max_refreshes} 次仍未扫码，已放弃")

    # -- 内部 -------------------------------------------------------------
    def _show(self, task: BindTask, attempt: int) -> None:
        url = build_connect_url(task.task_id, self.source)
        self.qr_url = url
        self.qr_data_url = ""
        if self.with_data_url:
            from .qr import qr_data_url

            self.qr_data_url = qr_data_url(url) or ""
        if self.on_qr is not None:
            self.on_qr(url, attempt, self.qr_data_url or None)

    def _status(self, text: str) -> None:
        if self.on_status is not None:
            self.on_status(text)

    def _failure(self, message: str) -> QrLoginResult:
        return QrLoginResult(connected=False, message=message, qr_url=self.qr_url)


# ---------------------------------------------------------------- 两阶段接口


@dataclass
class QrSession:
    """一个后台扫码会话的状态。"""

    account: str
    state: str = "starting"
    """``starting`` / ``waiting`` / ``connected`` / ``failed`` / ``cancelled``。"""
    message: str = ""
    qr_url: str = ""
    qr_data_url: str = ""
    created_at: str = ""
    updated_at: str = ""
    refreshes: int = 0
    result: QrLoginResult | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    done: threading.Event = field(default_factory=threading.Event, repr=False)
    qr_ready: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    def to_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "account": self.account,
            "state": self.state,
            "message": self.message,
            "qrUrl": self.qr_url,
            "qrDataUrl": self.qr_data_url or None,
            "refreshes": self.refreshes,
            "connected": self.state == "connected",
            "updatedAt": self.updated_at,
        }
        if self.result is not None and self.result.connected:
            # AppID 不是密钥，可以给前端看；AppSecret 只在 include_secret 时才给
            payload["appId"] = self.result.app_id
            payload["userOpenid"] = self.result.user_openid
            if include_secret:
                payload["credentials"] = self.result.credentials
        return payload


class QrLoginManager:
    """两阶段扫码登录：``start`` 立刻返回二维码，``wait`` 阻塞等结果。

    对应参考实现的 ``start_qr_login`` / ``wait_qr_login`` / ``cancel_qr_login``：
    后端（网页 / 机器人后台）拿二维码去展示，扫完再问结果，
    这样「出示二维码」和「等用户扫」不必卡在同一个 HTTP 请求里。
    """

    def __init__(
        self,
        protocol: QQBotProtocol,
        *,
        source: str = DEFAULT_SOURCE,
        with_data_url: bool = True,
        on_connected: Callable[[QrLoginResult], None] | None = None,
        on_event: Callable[[str], None] | None = None,
        flow_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.protocol = protocol
        self.source = source
        self.with_data_url = with_data_url
        self.on_connected = on_connected
        self.on_event = on_event
        self.flow_kwargs = dict(flow_kwargs or {})
        self.first_qr_timeout = float(self.flow_kwargs.pop("first_qr_timeout", 5.0))
        self._sessions: dict[str, QrSession] = {}
        self._lock = threading.Lock()

    # -- 阶段一 -----------------------------------------------------------
    def start(self, account: str = "default", *, source: str | None = None) -> dict[str, Any]:
        """新建会话并立刻返回（等第一张二维码生成，最多 ``first_qr_timeout`` 秒）。"""
        with self._lock:
            old = self._sessions.get(account)
            if old is not None and not old.done.is_set():
                old.cancel_event.set()
            session = QrSession(account=account, created_at=_stamp())
            session.updated_at = session.created_at
            self._sessions[account] = session
            session.thread = threading.Thread(
                target=self._run_session,
                args=(session, source or self.source),
                name=f"qqbot-qr-{account}",
                daemon=True,
            )
            session.thread.start()

        # 让调用方拿到的 qrDataUrl 尽量不是空的（参考实现这里会返回 null，稍微难用）
        session.qr_ready.wait(timeout=self.first_qr_timeout)
        return session.to_dict()

    # -- 阶段二 -----------------------------------------------------------
    def wait(
        self, account: str = "default", *, timeout: float | None = None, include_secret: bool = True
    ) -> dict[str, Any]:
        """等结果。会话结束后**从表里移除**（同一会话只能 wait 一次，同参考实现）。"""
        session = self._get(account)
        if session is None:
            return {
                "connected": False,
                "message": "没有正在进行的登录会话，请先调用 /qr/start。",
            }
        finished = session.done.wait(timeout=timeout)
        if not finished:
            payload = session.to_dict(include_secret=False)
            payload["message"] = payload.get("message") or "还在等用户扫码…"
            return payload
        self._pop(account)
        return session.to_dict(include_secret=include_secret)

    def status(self, account: str = "default", *, include_secret: bool = False) -> dict[str, Any]:
        session = self._get(account)
        if session is None:
            return {"state": "idle", "connected": False, "message": "没有进行中的登录会话"}
        return session.to_dict(include_secret=include_secret)

    def cancel(self, account: str = "default") -> dict[str, Any]:
        session = self._pop(account)
        if session is None:
            return {"cancelled": False, "message": "没有进行中的登录会话"}
        session.cancel_event.set()
        session.state = "cancelled"
        session.message = "已取消"
        session.done.set()
        return {"cancelled": True, "message": "已取消扫码登录"}

    def close(self) -> None:
        """中止所有会话（进程退出前调用）。"""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.cancel_event.set()

    # -- 内部 -------------------------------------------------------------
    def _run_session(self, session: QrSession, source: str) -> None:
        flow = QrLoginFlow(
            self.protocol,
            source=source,
            with_data_url=self.with_data_url,
            cancel=session.cancel_event,
            on_qr=lambda url, attempt, data_url: self._on_qr(session, url, attempt, data_url),
            on_status=lambda text: self._on_status(session, text),
            **self.flow_kwargs,
        )
        try:
            result = flow.run()
        except Exception as exc:  # noqa: BLE001 - 后台线程不能把异常丢掉
            result = QrLoginResult(
                connected=False, message=f"扫码登录异常：{type(exc).__name__}: {exc}"
            )
        session.result = result
        session.refreshes = result.refreshes
        session.qr_url = result.qr_url or session.qr_url
        if result.connected:
            session.state = "connected"
            session.message = result.message
            if self.on_connected is not None:
                try:
                    self.on_connected(result)
                except Exception as exc:  # noqa: BLE001
                    session.message = f"{result.message}（但保存凭证失败：{exc}）"
        elif session.cancel_event.is_set():
            session.state = "cancelled"
            session.message = result.message or "已取消"
        else:
            session.state = "failed"
            session.message = result.message or "扫码登录失败"
        session.updated_at = _stamp()
        session.qr_ready.set()
        session.done.set()
        self._emit(f"[qqbot:qr] {session.account} → {session.state}: {session.message}")

    def _on_qr(self, session: QrSession, url: str, attempt: int, data_url: str | None) -> None:
        session.qr_url = url
        session.qr_data_url = data_url or ""
        session.state = "waiting"
        session.message = f"请使用手机 QQ 扫描二维码完成绑定（第 {attempt} 张）"
        session.updated_at = _stamp()
        session.qr_ready.set()
        self._emit(f"[qqbot:qr] 二维码已就绪（第 {attempt} 张）")

    def _on_status(self, session: QrSession, text: str) -> None:
        session.message = text
        session.updated_at = _stamp()
        self._emit(f"[qqbot:qr] {text}")

    def _emit(self, text: str) -> None:
        if self.on_event is not None:
            self.on_event(text)

    def _get(self, account: str) -> QrSession | None:
        with self._lock:
            return self._sessions.get(account)

    def _pop(self, account: str) -> QrSession | None:
        with self._lock:
            return self._sessions.pop(account, None)


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


__all__ = [
    "DEFAULT_MAX_ERRORS",
    "DEFAULT_MAX_REFRESHES",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_QR_TIMEOUT",
    "DEFAULT_SOURCE",
    "QrCallback",
    "QrLoginFlow",
    "QrLoginManager",
    "QrLoginResult",
    "QrSession",
    "StatusCallback",
]
