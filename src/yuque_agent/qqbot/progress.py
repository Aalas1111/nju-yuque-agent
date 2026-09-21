"""分段发送 + 保活心跳：把 QQ 侧的发消息从「流式幻觉」掰回接口的真实形状。

QQ 的开放平台**不是流式接口**：没有 token 级推送，一次 REST 调用就是一条完整消息。
参考实现里那个 ``StreamSession``（C2C 打字机）走的是另一套 ``stream_messages`` 接口，
我们不用它。正确做法是：

1. **一段助手输出 = 一条消息**：LLM 一次回复里的 ``content`` 我们本来就是非流式拿到的，
   拿全了再整段发出去，然后继续下一步（不在 token 级别发、也不拆成好几条刷屏）；
2. **工具调用不发消息**，它只用来回答「现在在干什么」；
3. **保活**：上一条消息之后 ``idle_seconds`` 内没有新消息，就发一条
   ``⏳ 正在进行：<当前工具/步骤>``——这样长任务不会让对话看起来已经死掉；
4. **同一条 ``msg_id`` 用递增的 ``msg_seq`` 连发**（这是 QQ 被动回复窗口内
   发多条消息的正规姿势），窗口过期才需要退回主动消息。

这一层不碰 LLM、不碰 agent 循环，只做「攒 → 发 → 保活」，
所以可以完全离线测试（注入假 client + 假时钟）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .client import Target

LogFn = Callable[[str], None]


@runtime_checkable
class MessageSink(Protocol):
    """真正发消息的那一端（``QQBotClient`` 满足它；测试里可以换成假的）。"""

    def send_text(self, target: Target, content: str, **kwargs: Any) -> dict[str, Any]: ...


@dataclass
class ProgressOptions:
    """分段发送与保活的参数。"""

    idle_seconds: float = 60.0
    """上一条消息之后多久没新消息就发保活。``0`` = 关闭保活。"""

    min_interval_seconds: float = 2.0
    """两条消息之间的最小间隔，避免模型连着吐短段时刷屏。"""

    max_chars: int = 1200
    """单条消息上限。超过就**按行**切成多条（单行本身超长才会硬切）。"""

    heartbeat_template: str = "⏳ 正在进行：{current}"
    """保活文案。``{current}`` 会被替换成当前工具/步骤。"""

    start_seq: int = 1
    """``msg_seq`` 起始值；同一条 ``msg_id`` 下必须唯一且递增。"""


class ProgressSender:
    """把一个 run 过程中的助手输出按段发到 QQ，并在空闲时保活。

    线程模型：agent 线程调 :meth:`segment` / :meth:`current`；
    :meth:`start` 起的看门狗线程负责保活。两者共用一个锁，**发送严格串行**，
    所以 ``msg_seq`` 不会重复、消息顺序也不会乱。
    """

    def __init__(
        self,
        client: MessageSink,
        target: Target,
        *,
        options: ProgressOptions | None = None,
        log: LogFn | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.target = target
        self.options = options or ProgressOptions()
        self.log = log
        self._clock = clock
        self._sleep = sleep

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seq = int(self.options.start_seq)
        self._last_sent_at: float | None = None
        self._current = ""
        self.sent = 0
        self.failed = 0
        self.heartbeats = 0
        self.last_error = ""

    # ══════════════════════════ 生命周期 ══════════════════════════
    def start(self) -> ProgressSender:
        """起看门狗。``idle_seconds<=0`` 时不起（保活关闭）。"""
        with self._lock:
            self._last_sent_at = self._clock()
        if self.options.idle_seconds > 0 and self._thread is None:
            self._thread = threading.Thread(
                target=self._watchdog, name="qqbot-progress-heartbeat", daemon=True
            )
            self._thread.start()
        return self

    def stop(self, *, flush: bool = False) -> None:
        """停看门狗（默认不补发）。``flush=True`` 时把当前操作最后发一条。"""
        if flush and self._current:
            self.segment(self.options.heartbeat_template.format(current=self._current))
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def stats(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "failed": self.failed,
            "heartbeats": self.heartbeats,
            "segments": self.sent - self.heartbeats,
            "last_error": self.last_error,
        }

    # ══════════════════════════ 对外：一段输出 ══════════════════════════
    def segment(self, text: str, *, label: str = "") -> int:
        """**一段助手输出结束** → 整段发出去（必要时按行切分成多条）。

        返回实际发出的消息条数；空段（或纯空白）直接忽略，不产生空消息。
        """
        chunks = split_segment(text, self.options.max_chars)
        if not chunks:
            return 0
        if label:
            chunks[0] = f"{label}\n{chunks[0]}"
        count = 0
        for chunk in chunks:
            self._wait_turn()
            if self._deliver(chunk):
                count += 1
        return count

    def current(self, description: str) -> None:
        """更新「现在在干什么」。**不发消息**，只影响保活文案。"""
        with self._lock:
            self._current = (description or "").strip()

    @property
    def current_operation(self) -> str:
        with self._lock:
            return self._current

    # ══════════════════════════ 内部 ══════════════════════════
    def _wait_turn(self) -> None:
        """两条消息之间至少隔 ``min_interval_seconds``。"""
        gap = self.options.min_interval_seconds
        if gap <= 0:
            return
        while True:
            with self._lock:
                last = self._last_sent_at
                if last is None:
                    return
                wait = gap - (self._clock() - last)
            if wait <= 0:
                return
            self._sleep(min(wait, gap))

    def _deliver(self, text: str, *, heartbeat: bool = False) -> bool:
        payload = (text or "").strip()
        if not payload:
            return False
        with self._lock:
            seq = self._seq
            self._seq += 1
            # 只有被动回复（有 msg_id）才带 msg_seq；主动消息不带这个字段
            kwargs = {"msg_seq": seq} if self.target.msg_id else {}
            try:
                self.client.send_text(self.target, payload, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 发不出去不能拖垮整个 run
                self.failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._log(f"[qqbot:progress] 第 {seq} 条发送失败：{self.last_error}")
                return False
            self.sent += 1
            if heartbeat:
                self.heartbeats += 1
            self._last_sent_at = self._clock()
        kind = "保活" if heartbeat else "分段"
        self._log(f"[qqbot:progress] {kind} #{seq} 已发送（{len(payload)} 字）")
        return True

    def _watchdog(self) -> None:
        """空闲超时就发一条保活。"""
        idle = self.options.idle_seconds
        step = max(0.2, min(1.0, idle / 4.0))
        while not self._stop.wait(step):
            with self._lock:
                current = self._current
                last = self._last_sent_at
            if not current:
                continue
            since = self._clock() - (last if last is not None else self._clock())
            if since < idle:
                continue
            self._deliver(self.options.heartbeat_template.format(current=current), heartbeat=True)

    def _log(self, text: str) -> None:
        if self.log is not None:
            self.log(text)


def split_segment(text: str, max_chars: int) -> list[str]:
    """把一段文本切成 ≤ ``max_chars`` 的几条，**优先按行切**（不切在行中间）。

    只有某一行本身超过上限时才会硬切那一行——这是接口硬限制，无法避免。
    """
    body = (text or "").strip()
    if not body:
        return []
    if max_chars <= 0 or len(body) <= max_chars:
        return [body]

    chunks: list[str] = []
    buffer = ""
    for line in body.splitlines(keepends=True):
        while len(line) > max_chars:
            if buffer:
                chunks.append(buffer.rstrip())
                buffer = ""
            chunks.append(line[:max_chars].rstrip())
            line = line[max_chars:]
        if len(buffer) + len(line) > max_chars:
            chunks.append(buffer.rstrip())
            buffer = line
        else:
            buffer += line
    if buffer.strip():
        chunks.append(buffer.rstrip())
    return [chunk for chunk in chunks if chunk.strip()]


__all__ = [
    "MessageSink",
    "ProgressOptions",
    "ProgressSender",
    "split_segment",
]
