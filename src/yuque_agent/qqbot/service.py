"""常驻服务：QQ 通知投递 + 入站命令（**不跑轮询**）。

```
   事件循环 ── 通知泵（每 N 秒 drain 一次）┐
            └─ WS 网关（收消息 → RouterAgent → 回一句）
   通知泵顺带看一眼 control/done/：/run、/archive 的回执在那儿取
```

轮询与归档**不在这里**——它们由核心的常驻进程（``yqa run`` / ``yuque-agent.service``）
唯一负责：``state.json`` 只能有一个写者，两个轮询进程互相覆盖快照是记过事故的
（``AGENTS.md`` §2.2、``docs/deploy.md`` §11）。

所以这里的 ``/run`` ``/archive`` ``/apply`` 只是往 ``control/requests/`` 写一条请求
（``docs/interface.md`` §1.2/§1.4），由核心消费：

* 跑轮询/归档的回执走 ``control/done/``，通知泵取回来发给发起人；
* 申请的回执是核心发的 ``accepted`` / ``rejected`` 通知（走通知队列，直投给本人）。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import clock
from ..config import Settings
from .agent import RouterAgent, register_default_workflows
from .bridge import NotifyBridge
from .client import QQBotClient, Target
from .config import QQBotConfig
from .control_client import drop_control_result, read_control_result, write_control_request
from .conversations import ConversationManager, first_question
from .events import InboundMessage
from .gateway import GatewayOptions, intents_from_env, run_gateway

DEFAULT_NOTIFY_INTERVAL = 5.0
"""通知泵间隔（秒）。顺带在这里取控制请求的回执，所以别调太大。"""


class QQBotService:
    """常驻服务：通知泵 + QQ 网关（命令处理）。"""

    def __init__(
        self,
        *,
        settings: Settings,
        bridge: NotifyBridge,
        config: QQBotConfig,
        qq_client: QQBotClient | None = None,
        log: Callable[[str], None] | None = None,
        notify_interval: float = DEFAULT_NOTIFY_INTERVAL,
    ) -> None:
        self.settings = settings
        self.bridge = bridge
        self.config = config
        self.qq_client = qq_client
        self.log = log
        self.notify_interval = notify_interval

        self.conversations = ConversationManager(conversations_dir=settings.conversations_dir)
        self.agent = RouterAgent(
            conversations=self.conversations,
            config=config,
            gateway=self,
            log=self._log,
        )
        register_default_workflows(self.agent)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        # 请求文件名 → {kind, target}：核心跑完后从 control/done/ 取回执
        self._control_watch: dict[str, dict[str, Any]] = {}

    # ══════════════════════ AgentGateway（工作流用） ══════════════════════
    def pending_notices(self) -> int:
        try:
            return self.bridge.pending_count()
        except OSError:  # pragma: no cover
            return 0

    def status(self) -> dict[str, Any]:
        """``/status`` 用。**只读**工作区（轮询/归档归核心进程，见 interface.md §1.3）。"""
        return {
            "repo": self.settings.repo,
            "workspace": str(self.settings.root),
            "watching": _core_daemon_alive(self.settings),
            "pending": self.pending_notices(),
            "last_run": _newest_run_summary(self.settings),
            "inbound": bool(self.qq_client) and self.config.inbound_enabled,
        }

    def request_run(
        self,
        *,
        archive: bool = False,
        requested_by: str = "",
        reply_target: Target | None = None,
    ) -> dict[str, Any]:
        """把「跑一轮」写成控制请求（由核心的常驻进程执行；单飞）。"""
        kind = "archive" if archive else "once"
        with self._lock:
            if self._control_watch:
                return {
                    "queued": False,
                    "message": "上一个请求还在跑（核心进程最多要等一分钟才开始），稍后再来。",
                    "request_id": "",
                }
            request: dict[str, Any] = {"kind": kind, "requested_by": requested_by}
            if reply_target is not None:
                request["target"] = {
                    "scope": reply_target.scope,
                    "target_id": reply_target.target_id,
                }
            try:
                request_id = write_control_request(self.settings, request)
            except OSError as exc:
                return {"queued": False, "message": f"写请求失败：{exc}", "request_id": ""}
            self._control_watch[request_id] = {"kind": kind, "target": reply_target}
        what = "归档（会动知识库结构）" if archive else "一轮轮询"
        return {
            "queued": True,
            "message": f"收到，已请核心进程跑{what}；跑完我把结论发给你（最多 1 分钟内开始）。",
            "request_id": request_id,
        }

    def request_apply(self, *, user_id: str) -> dict[str, Any]:
        """开始一次交互式申请会话。"""
        session = self.conversations.get_or_create(user_id)
        return {"message": first_question(session)}

    # ══════════════════════════ 常驻 ══════════════════════════
    def serve(self, *, inbound: bool = True) -> None:
        """阻塞运行：通知泵 + （可选）WS 网关。"""
        self.settings.ensure_dirs()
        self.bridge.ensure_dirs()
        try:
            asyncio.run(self._serve_async(inbound))
        except KeyboardInterrupt:
            raise
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

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
            try:
                await asyncio.to_thread(self._drain_control_results)
            except Exception as exc:  # noqa: BLE001
                self._log(f"[qqbot:serve] 控制回执异常：{type(exc).__name__}: {exc}")
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

    def _drain_notices(self) -> None:
        results = self.bridge.drain()
        for item in results:
            if item.status == "failed":
                self._log(f"[qqbot:serve] 通知投递失败：{item.describe()}")

    def _drain_control_results(self) -> None:
        """把核心跑完的 ``/run``、``/archive`` 回执发给发起人。"""
        with self._lock:
            watching = dict(self._control_watch)
        for request_id, item in watching.items():
            record = read_control_result(self.settings, request_id)
            if record is None:
                continue
            text = _control_reply(record)
            target = item.get("target")
            if isinstance(target, Target) and self.qq_client is not None:
                try:
                    self.qq_client.send_text(target, text)
                except Exception as exc:  # noqa: BLE001 - 主动消息失败只记日志
                    self._log(f"[qqbot:control] 回执发送失败：{type(exc).__name__}: {exc}")
            else:
                self._log(f"[qqbot:control] {request_id}：{text}")
            with self._lock:
                self._control_watch.pop(request_id, None)
            drop_control_result(self.settings, request_id)

    # -- 入站 -------------------------------------------------------------
    def handle_inbound(self, message: InboundMessage) -> dict[str, Any]:
        """收到一条 QQ 消息：交给 RouterAgent 统一处理。跑在线程池里。"""
        self._log(f"[qqbot:in] {message.kind} {message.sender_id}: {message.text[:80]!r}")
        result = self.agent.process(message, settings=self.settings)
        out: dict[str, Any] = {"handled": True, "command": result.command, "reply": result.reply}
        if result.silent or not result.reply:
            out["silent"] = True
            return out
        target = message.reply_target
        if target is None:
            self._log("[qqbot:in] 这条消息没有可回复的目标（缺 message_id？），只记日志")
            return out
        if self.qq_client is None:
            return out
        try:
            self.qq_client.send_text(target, result.reply)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[qqbot:in] 回复失败：{type(exc).__name__}: {exc}")
        return out

    # -- 杂项 -------------------------------------------------------------
    def _log(self, text: str) -> None:
        if self.log is not None:
            self.log(text)


# ---------------------------------------------------------------- 只读辅助


def _core_daemon_alive(settings: Settings) -> bool:
    """核心常驻进程还活着吗？看 ``state.json`` 是不是还在被写（只读判断）。"""
    try:
        age = clock.now().timestamp() - (settings.root / "state.json").stat().st_mtime
    except OSError:
        return False
    return age < max(180.0, settings.interval * 3)


def _newest_run_summary(settings: Settings) -> dict[str, Any]:
    """从 ``runs/`` 里读最近一次 run 的结论（interface.md §1.3 允许只读的目录）。"""
    runs: Path = settings.runs_dir
    if not runs.exists():
        return {}
    newest: tuple[float, Path] | None = None
    for path in runs.glob("*/result.json"):
        try:
            mtime = path.stat().st_mtime
        except OSError:  # pragma: no cover
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, path)
    if newest is None:
        return {}
    try:
        data = json.loads(newest[1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "kind": data.get("kind", ""),
        "verdict": data.get("verdict", ""),
        "summary": data.get("summary", ""),
        "run_id": data.get("run_id", ""),
        "at": datetime.fromtimestamp(newest[0], tz=clock.TZ).strftime("%m-%d %H:%M"),
    }


def _control_reply(record: dict[str, Any]) -> str:
    ok = bool(record.get("ok"))
    summary = str(record.get("summary") or "").strip()
    error = str(record.get("error") or "").strip()
    run_id = str(record.get("run_id") or "")
    if ok:
        text = summary or "跑完了（没有摘要）"
    else:
        text = f"跑失败：{error or '未知错误'}"
    if run_id:
        text += f"\nrun: {run_id}"
    return text


__all__ = [
    "DEFAULT_NOTIFY_INTERVAL",
    "QQBotService",
]
