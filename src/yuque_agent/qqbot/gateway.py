"""QQ WebSocket 网关：**收**消息那一半（发消息在 :mod:`.client`）。

对应参考实现 ``qqbot_backend_sdk/gateway/``。这里只保留本项目需要的部分：
IDENTIFY / RESUME、心跳、断线重连、事件归一化（:mod:`.events`），不实现分片/压缩等细节。

``websockets`` 是可选依赖：没装时这里会抛一条**能看懂**的错误，而不是 ImportError 崩掉。
``yqa qq serve --no-inbound`` 不需要它。

设计上的一个关键点：**handler 同步执行、但跑在线程池里**。
因为 handler 可能要跑一整轮 LLM（几十秒），直接在事件循环里跑会把心跳饿死、触发服务端踢人。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .client import QQBotClient
from .events import EVENT_READY, EVENT_RESUMED, InboundMessage, parse_event
from .protocol import QQBotError

#: intents（1 << 25 = 群 @ 与 C2C 私聊消息，本项目默认只要这个）。
INTENT_GUILDS = 1 << 0
INTENT_GUILD_MEMBERS = 1 << 1
INTENT_DIRECT_MESSAGE = 1 << 12
INTENT_GROUP_AND_C2C = 1 << 25
INTENT_INTERACTION = 1 << 26

DEFAULT_INTENTS = INTENT_GROUP_AND_C2C

#: 重连退避（秒），最后一个值重复使用。
RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)

CLOSE_AUTH_FAILED = 4004
CLOSE_RATE_LIMITED = 4008


class GatewayOp:
    DISPATCH = 0
    HEARTBEAT = 1
    IDENTIFY = 2
    RESUME = 6
    RECONNECT = 7
    INVALID_SESSION = 9
    HELLO = 10
    HEARTBEAT_ACK = 11


class _FatalGatewayError(RuntimeError):
    """再连也没用的错误（鉴权失败等）。"""


def intents_from_env(default: int = DEFAULT_INTENTS, env: Mapping[str, str] | None = None) -> int:
    """``YQA_QQ_INTENTS`` 覆盖默认 intents（十进制或 ``0x…`` 都认，认不出就用默认）。"""
    raw = ((env if env is not None else os.environ).get("YQA_QQ_INTENTS") or "").strip()
    if not raw:
        return default
    try:
        return int(raw, 0)
    except ValueError:
        return default


@dataclass
class GatewayOptions:
    """网关的行为参数。"""

    intents: int = DEFAULT_INTENTS
    handler: Callable[[InboundMessage], Any] | None = None
    """收到消息时调用。**同步**函数，跑在线程池里；返回协程则 await 它。"""

    on_ready: Callable[[dict[str, Any]], None] | None = None
    on_log: Callable[[str], None] | None = None
    should_stop: Callable[[], bool] | None = None
    """返回 True 就优雅退出（用同步的 ``threading.Event.is_set`` 最方便）。"""

    max_attempts: int = 100
    reconnect: bool = True
    close_timeout: float = 5.0
    max_message_size: int = 4 * 1024 * 1024

    def log(self, text: str) -> None:
        if self.on_log is not None:
            self.on_log(text)

    def stopped(self) -> bool:
        return bool(self.should_stop is not None and self.should_stop())


@dataclass
class _SessionState:
    session_id: str = ""
    seq: int | None = None
    ready_at: str = ""
    events: int = field(default=0, repr=False)


def _connect_factory() -> Any:
    """拿到 websockets 的 ``connect``（兼容新旧版本导入路径）。"""
    try:
        from websockets.asyncio.client import connect  # type: ignore[import-not-found]
    except ImportError:
        try:
            from websockets import connect  # type: ignore[no-redef]
        except ImportError as exc:
            raise QQBotError(
                "QQ 网关需要 websockets：pip install websockets"
                "（只投递通知的话用 `yqa qq notify` 或 `yqa qq serve --no-inbound`，不需要它）"
            ) from exc
    return connect


async def run_gateway(client: QQBotClient, options: GatewayOptions | None = None) -> None:
    """连上网关并一直收消息，直到 :attr:`GatewayOptions.should_stop` 为真。"""
    opts = options or GatewayOptions()
    connect = _connect_factory()
    state = _SessionState()
    attempt = 0

    while not opts.stopped():
        try:
            await _run_connection(client, opts, state, connect)
        except _FatalGatewayError as exc:
            opts.log(f"[qqbot:gateway] 致命错误，不再重连：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 断线是常态，重连就好
            opts.log(f"[qqbot:gateway] 连接断开：{type(exc).__name__}: {exc}")
        if opts.stopped() or not opts.reconnect:
            return
        attempt += 1
        if attempt > opts.max_attempts:
            opts.log(f"[qqbot:gateway] 重连 {opts.max_attempts} 次仍未成功，放弃")
            return
        delay = RECONNECT_DELAYS[min(attempt - 1, len(RECONNECT_DELAYS) - 1)]
        delay *= 0.7 + random.random() * 0.6  # 抖动，避免所有实例同时回来
        opts.log(f"[qqbot:gateway] {delay:.1f} 秒后重连（第 {attempt} 次）")
        await _sleep(delay, opts)


async def _run_connection(
    client: QQBotClient, opts: GatewayOptions, state: _SessionState, connect: Any
) -> None:
    url = await asyncio.to_thread(client.gateway_url)
    opts.log(f"[qqbot:gateway] 正在连接 {url[:60]}…")
    try:
        socket = await connect(
            url,
            max_size=opts.max_message_size,
            open_timeout=15,
            close_timeout=opts.close_timeout,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"连接网关失败：{exc}") from exc

    async with socket:
        hello = await _recv_json(socket, timeout=15)
        if hello.get("op") != GatewayOp.HELLO:
            opts.log(f"[qqbot:gateway] 第一帧不是 HELLO（op={hello.get('op')}），仍继续")
        interval = float((hello.get("d") or {}).get("heartbeat_interval") or 40000) / 1000
        heartbeat = asyncio.create_task(_heartbeat(socket, interval, state))
        try:
            await _identify(socket, client, opts, state)
            async for raw in socket:
                payload = _loads(raw)
                if payload is None:
                    continue
                if payload.get("s") is not None:
                    state.seq = int(payload["s"])
                if payload.get("op") == GatewayOp.HEARTBEAT:
                    # 服务端要求立刻回一个心跳
                    await socket.send(json.dumps({"op": GatewayOp.HEARTBEAT, "d": state.seq}))
                    continue
                action = await _handle(payload, opts, state)
                if action == "reconnect":
                    return
        except Exception as exc:  # noqa: BLE001
            code = _close_code(exc)
            if code == CLOSE_AUTH_FAILED:
                raise _FatalGatewayError(
                    "网关鉴权失败（4004）：AppID/AppSecret 是否正确？机器人在开放平台上线了吗？"
                ) from exc
            if code == CLOSE_RATE_LIMITED:
                opts.log("[qqbot:gateway] 被限流（4008），60 秒后再连")
                await _sleep(60.0, opts)
            raise
        finally:
            heartbeat.cancel()


async def _identify(
    socket: Any, client: QQBotClient, opts: GatewayOptions, state: _SessionState
) -> None:
    token = await asyncio.to_thread(client.get_access_token)
    if state.session_id:
        payload = {
            "op": GatewayOp.RESUME,
            "d": {
                "token": f"QQBot {token}",
                "session_id": state.session_id,
                "seq": state.seq,
            },
        }
        opts.log(f"[qqbot:gateway] RESUME（session={state.session_id[:12]}… seq={state.seq}）")
    else:
        payload = {
            "op": GatewayOp.IDENTIFY,
            "d": {
                "token": f"QQBot {token}",
                "intents": int(opts.intents),
                "properties": {
                    "$os": "linux",
                    "$browser": "yuque-agent",
                    "$device": "yuque-agent",
                },
            },
        }
        opts.log(f"[qqbot:gateway] IDENTIFY（intents={opts.intents}）")
    await socket.send(json.dumps(payload))


async def _handle(payload: dict[str, Any], opts: GatewayOptions, state: _SessionState) -> str:
    op = payload.get("op")
    if op == GatewayOp.DISPATCH:
        event_type = str(payload.get("t") or "")
        data = payload.get("d")
        if event_type == EVENT_READY:
            state.session_id = str((data or {}).get("session_id") or "")
            opts.log(f"[qqbot:gateway] READY（session={state.session_id[:12]}…）")
            if opts.on_ready is not None:
                opts.on_ready(data if isinstance(data, dict) else {})
            return "ok"
        if event_type == EVENT_RESUMED:
            opts.log("[qqbot:gateway] RESUMED")
            if opts.on_ready is not None:
                opts.on_ready(data if isinstance(data, dict) else {})
            return "ok"
        message = parse_event(event_type, data)
        if message is not None:
            state.events += 1
            await _dispatch(message, opts)
        return "ok"
    if op == GatewayOp.RECONNECT:
        opts.log("[qqbot:gateway] 服务端要求重连")
        return "reconnect"
    if op == GatewayOp.INVALID_SESSION:
        opts.log("[qqbot:gateway] 会话失效，清掉 session 重新 IDENTIFY")
        state.session_id = ""
        state.seq = None
        await asyncio.sleep(2)
        return "reconnect"
    if op == GatewayOp.HEARTBEAT_ACK:
        return "ok"
    opts.log(f"[qqbot:gateway] 收到未知 op={op}，忽略")
    return "ok"


async def _dispatch(message: InboundMessage, opts: GatewayOptions) -> None:
    if opts.handler is None:
        return
    try:
        result = await asyncio.to_thread(opts.handler, message)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:  # noqa: BLE001 - 一条消息处理失败不能拖垮网关
        opts.log(f"[qqbot:gateway] 处理消息失败：{type(exc).__name__}: {exc}")


async def _heartbeat(socket: Any, interval: float, state: _SessionState) -> None:
    jitter = 0.85 + random.random() * 0.3
    while True:
        await asyncio.sleep(max(1.0, interval * jitter))
        try:
            await socket.send(json.dumps({"op": GatewayOp.HEARTBEAT, "d": state.seq}))
        except Exception:  # noqa: BLE001 - 发送失败就结束心跳，交给重连
            return


async def _sleep(seconds: float, opts: GatewayOptions) -> None:
    """可被 ``should_stop`` 打断的 sleep。"""
    waited = 0.0
    while waited < seconds:
        if opts.stopped():
            return
        step = min(0.5, seconds - waited)
        await asyncio.sleep(step)
        waited += step


async def _recv_json(socket: Any, *, timeout: float) -> dict[str, Any]:
    raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
    payload = _loads(raw)
    if payload is None:
        raise RuntimeError("网关第一帧不是合法 JSON")
    return payload


def _loads(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _close_code(exc: Exception) -> int | None:
    for candidate in (
        getattr(exc, "code", None),
        getattr(getattr(exc, "rcvd", None), "code", None),
        getattr(getattr(exc, "sent", None), "code", None),
    ):
        if isinstance(candidate, int):
            return candidate
    return None


__all__ = [
    "CLOSE_AUTH_FAILED",
    "CLOSE_RATE_LIMITED",
    "DEFAULT_INTENTS",
    "GatewayOp",
    "GatewayOptions",
    "INTENT_GROUP_AND_C2C",
    "INTENT_INTERACTION",
    "RECONNECT_DELAYS",
    "intents_from_env",
    "run_gateway",
]
