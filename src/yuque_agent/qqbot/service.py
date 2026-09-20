"""常驻服务：把「agent 轮询」和「QQ 收发」接在一个进程里。

```
                ┌──────────── 线程：agent 工作循环 ────────────┐
  /run  /archive│  队列 → runner.poll_once / archive_once      │
  ──────────────►│  否则 → watcher.tick()（轮询 + 每周归档）    │
   (入站命令)     │  每轮结束 → bridge.drain()（把通知投出去）    │
                └──────────────────────┬───────────────────────┘
                                       │ outbox/notify/pending
   事件循环 ── 通知泵（每 N 秒 drain 一次）│
           └─ WS 网关（收消息 → CommandRouter → 回一句）
```

为什么要分成「一个 agent 线程 + 一个事件循环」：

* agent 那一半是**同步**的（httpx + LLM + 语雀），而且一次要跑几十秒；
* QQ 那一半是 **asyncio** 的（websockets 网关）。
* 两边共用的只有 ``outbox/notify/`` 这个目录，所以不需要任何跨事件循环的魔法。

**所有会跑 LLM 的入口都排进同一个队列**（包括定时轮询、``/run``、``/archive``），
由唯一的 agent 线程串行执行 —— 永远不会有两个 LLM run 同时动同一个知识库。
这也是本项目「能力边界」在工程上的落点：并发本身就是一种失控。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config import Settings
from ..runner import Runner
from ..watcher import Watcher
from .bridge import NotifyBridge
from .client import QQBotClient, Target
from .commands import AgentGateway, CommandRouter
from .config import QQBotConfig
from .events import InboundMessage
from .gateway import GatewayOptions, intents_from_env, run_gateway

DEFAULT_NOTIFY_INTERVAL = 5.0
"""通知泵间隔（秒）。比轮询间隔小得多，这样社员几乎立刻收到通知。"""


@dataclass
class AgentRequest:
    """一次「请 agent 干活」的请求（由 QQ 命令或 CLI 塞进队列）。"""

    kind: str
    force: bool = False
    requested_by: str = ""
    source: str = "qq"
    request_id: str = ""
    created_at: str = ""

    def describe(self) -> str:
        who = self.requested_by or "(未知)"
        return f"{self.kind} ← {self.source}:{who} (force={self.force})"


class QQBotService:
    """常驻服务：agent 工作线程 + QQ 通知泵/网关。"""

    def __init__(
        self,
        *,
        settings: Settings,
        runner: Runner,
        watcher: Watcher,
        bridge: NotifyBridge,
        config: QQBotConfig,
        qq_client: QQBotClient | None = None,
        log: Callable[[str], None] | None = None,
        notify_interval: float = DEFAULT_NOTIFY_INTERVAL,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.watcher = watcher
        self.bridge = bridge
        self.config = config
        self.qq_client = qq_client
        self.log = log
        self.notify_interval = notify_interval

        self.router = CommandRouter(config=config, gateway=self, log=log)

        self._queue: deque[AgentRequest] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._busy = False
        self._watch_started = False
        self._last_run: dict[str, Any] = {}
        self._served = 0

    # ══════════════════════ AgentGateway（给命令路由用） ══════════════════════
    def pending_notices(self) -> int:
        try:
            return self.bridge.pending_count()
        except OSError:  # pragma: no cover
            return 0

    def request_run(self, *, archive: bool = False, requested_by: str = "") -> dict[str, Any]:
        """把一次跑轮次排进队列（不阻塞）。真正的执行在 agent 线程里。"""
        with self._lock:
            if self._busy or self._queue:
                return {
                    "queued": False,
                    "message": "agent 正在忙（或队列里已经有一次了），等它跑完再来。",
                    "request_id": "",
                }
            request = AgentRequest(
                kind="archive" if archive else "polling",
                force=True,
                requested_by=requested_by,
                source="qq",
                request_id=uuid.uuid4().hex[:12],
                created_at=_stamp(),
            )
            self._queue.append(request)
        self._wake.set()
        what = "归档会话（会动知识库结构）" if archive else "一轮轮询"
        return {
            "queued": True,
            "message": f"收到，已排队跑{what}；跑完我把结论发给你（约几十秒到几分钟）。",
            "request_id": request.request_id,
        }

    def status(self) -> dict[str, Any]:
        return {
            "repo": self.settings.repo,
            "workspace": str(self.settings.root),
            "watching": self._watch_started,
            "pending": self.pending_notices(),
            "last_run": dict(self._last_run),
            "inbound": bool(self.qq_client) and self.config.inbound_enabled,
            "queued": len(self._queue),
            "busy": self._busy,
            "served": self._served,
            "config": str(self.config.path) if self.config.path else "",
        }

    # ══════════════════════════ 常驻 ══════════════════════════
    def serve(self, *, watch: bool = True, inbound: bool = True) -> None:
        """阻塞运行：agent 线程 + （可选）通知泵与 WS 网关。"""
        self.settings.ensure_dirs()
        self.bridge.ensure_dirs()
        worker = threading.Thread(
            target=self.work_loop, args=(watch,), name="yuque-agent-worker", daemon=True
        )
        worker.start()
        try:
            asyncio.run(self._serve_async(inbound))
        except KeyboardInterrupt:
            raise
        finally:
            self.stop()
            worker.join(timeout=10)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # -- agent 线程 -------------------------------------------------------
    def work_loop(self, watch: bool = True) -> None:
        """唯一会跑 LLM 的循环。可以单独调（测试里就是这么用的）。"""
        self._watch_started = watch
        self._log(
            f"[qqbot:serve] agent 线程启动："
            f"{'轮询 + 归档' if watch else '只处理队列请求'}；"
            f"每 {self.settings.interval}s 一轮"
        )
        while not self._stop.is_set():
            request = self._take()
            if request is not None:
                self._execute(request)
            elif watch:
                try:
                    self.watcher.tick()
                except Exception as exc:  # noqa: BLE001 - 常驻进程不能因为一轮失败就死
                    self._log(
                        f"[qqbot:serve] 本轮异常（已忽略并继续）：{type(exc).__name__}: {exc}"
                    )
                self._record_watcher()
            try:
                self._drain_notices()
            except Exception as exc:  # noqa: BLE001
                self._log(f"[qqbot:serve] 投递通知异常（已忽略）：{type(exc).__name__}: {exc}")
            self._wake.wait(timeout=max(1, int(self.settings.interval)))
            self._wake.clear()
        self._log("[qqbot:serve] agent 线程退出")

    def _take(self) -> AgentRequest | None:
        with self._lock:
            if not self._queue:
                return None
            return self._queue.popleft()

    def _execute(self, request: AgentRequest) -> None:
        with self._lock:
            self._busy = True
        self._log(f"[qqbot:serve] 开始执行 {request.describe()}")
        started = time.monotonic()
        result = None
        try:
            if request.kind == "archive":
                result = self.runner.archive_once()
            else:
                result = self.runner.poll_once(force=request.force)
        except Exception as exc:  # noqa: BLE001 - 失败也要回话，别让用户干等
            self._log(f"[qqbot:serve] 执行失败：{type(exc).__name__}: {exc}")
            self._announce(request, f"跑失败了：{type(exc).__name__}: {exc}")
            return
        finally:
            with self._lock:
                self._busy = False
        elapsed = time.monotonic() - started
        self._served += 1
        if result is None:
            self._record_watcher()
            self._announce(request, "这一轮没有变化，没有唤醒 LLM（0 token）。")
            return
        self._set_last_run(result)
        self._announce(request, _summarize(result, elapsed))

    def _record_watcher(self) -> None:
        result = getattr(self.watcher, "last_result", None)
        if result is not None:
            self._set_last_run(result)

    def _set_last_run(self, result: Any) -> None:
        self._last_run = {
            "kind": getattr(result, "kind", ""),
            "verdict": getattr(result, "verdict", ""),
            "summary": getattr(result, "summary", ""),
            "run_id": getattr(result, "run_id", ""),
            "at": _stamp(),
        }

    def _announce(self, request: AgentRequest, text: str) -> None:
        """把结果推给发起人。QQ 主动消息有配额限制，失败只记日志。"""
        if request.source != "qq" or not request.requested_by:
            self._log(f"[qqbot:serve] 结果（{request.requested_by or '本地'}）：{text}")
            return
        if self.qq_client is None:
            return
        try:
            self.qq_client.send_text(Target("c2c", request.requested_by), text)
        except Exception as exc:  # noqa: BLE001 - 主动推送被拒是常态
            self._log(
                f"[qqbot:serve] 结果没能推给 {request.requested_by}（{type(exc).__name__}: {exc}）；"
                "结论仍在 runs/<run_id>/ 里"
            )

    def _drain_notices(self) -> None:
        results = self.bridge.drain()
        for item in results:
            if item.status == "failed":
                self._log(f"[qqbot:serve] 通知投递失败：{item.describe()}")

    # -- 事件循环 ---------------------------------------------------------
    async def _serve_async(self, inbound: bool) -> None:
        tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(self._notify_pump(), name="qqbot-notify-pump")
        ]
        if inbound and self.qq_client is not None and self.config.inbound_enabled:
            tasks.append(asyncio.create_task(self._gateway_loop(), name="qqbot-gateway"))
        else:
            reason = (
                "没有绑定机器人"
                if self.qq_client is None
                else ("配置里关掉了入站" if not self.config.inbound_enabled else "--no-inbound")
            )
            self._log(f"[qqbot:serve] 入站关闭（{reason}），只投递通知")
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()

    async def _notify_pump(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._drain_notices)
            except Exception as exc:  # noqa: BLE001
                self._log(f"[qqbot:serve] 通知泵异常：{type(exc).__name__}: {exc}")
            await asyncio.sleep(max(1.0, self.notify_interval))

    async def _gateway_loop(self) -> None:
        assert self.qq_client is not None
        options = GatewayOptions(
            intents=intents_from_env(),
            handler=self.handle_inbound,
            on_log=self._log,
            should_stop=lambda: self._stop.is_set(),
        )
        await run_gateway(self.qq_client, options)

    # -- 入站 -------------------------------------------------------------
    def handle_inbound(self, message: InboundMessage) -> dict[str, Any]:
        """收到一条 QQ 消息：过命令路由，能回就回。跑在线程池里（可能很慢）。"""
        self._log(f"[qqbot:in] {message.kind} {message.sender_id}: {message.text[:80]!r}")
        result = self.router.dispatch(message)
        if result.silent or not result.reply:
            return result.to_dict()
        target = message.reply_target
        if target is None:
            self._log("[qqbot:in] 这条消息没有可回复的目标（缺 message_id？），只记日志")
            return result.to_dict()
        if self.qq_client is None:
            return result.to_dict()
        try:
            self.qq_client.send_text(target, result.reply)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[qqbot:in] 回复失败：{type(exc).__name__}: {exc}")
        return result.to_dict()

    # -- 杂项 -------------------------------------------------------------
    def _log(self, text: str) -> None:
        if self.log is not None:
            self.log(text)


def _summarize(result: Any, elapsed: float) -> str:
    bits = [
        f"kind={getattr(result, 'kind', '?')}",
        f"verdict={getattr(result, 'verdict', '') or '—'}",
        f"steps={getattr(result, 'steps', '?')}",
        f"tools={getattr(result, 'tool_calls', '?')}",
        f"{elapsed:.0f}s",
    ]
    emitted = getattr(result, "emitted", None) or []
    if emitted:
        kinds = "、".join(str(item.get("type")) for item in emitted)
        bits.append(f"产出={kinds}")
    error = getattr(result, "error", "")
    lines = [f"跑完了：{getattr(result, 'summary', '') or '(无摘要)'}", " · ".join(bits)]
    if error:
        lines.append(f"⚠️ {error}")
    run_id = getattr(result, "run_id", "")
    if run_id:
        lines.append(f"run: {run_id}")
    return "\n".join(lines)


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


__all__ = ["DEFAULT_NOTIFY_INTERVAL", "AgentGateway", "AgentRequest", "QQBotService"]
