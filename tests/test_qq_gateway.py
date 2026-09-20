"""网关协议处理测试（不建 socket）：intents、帧解析、事件分发、会话失效。"""

from __future__ import annotations

import asyncio
import json

from yuque_agent.qqbot.events import InboundMessage
from yuque_agent.qqbot.gateway import (
    DEFAULT_INTENTS,
    GatewayOp,
    GatewayOptions,
    _handle,
    _loads,
    _SessionState,
    intents_from_env,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- intents


def test_intents_default() -> None:
    assert intents_from_env(env={}) == DEFAULT_INTENTS


def test_intents_env_override_decimal_and_hex() -> None:
    assert intents_from_env(env={"YQA_QQ_INTENTS": "33554433"}) == 33554433
    assert intents_from_env(env={"YQA_QQ_INTENTS": "0x2000000"}) == 0x2000000


def test_intents_env_garbage_falls_back() -> None:
    assert intents_from_env(env={"YQA_QQ_INTENTS": "nonsense"}) == DEFAULT_INTENTS
    assert intents_from_env(env={"YQA_QQ_INTENTS": "  "}) == DEFAULT_INTENTS


# ---------------------------------------------------------------- 帧解析


def test_loads_accepts_bytes_and_str() -> None:
    payload = {"op": 10, "d": {"heartbeat_interval": 40000}}
    assert _loads(json.dumps(payload)) == payload
    assert _loads(json.dumps(payload).encode("utf-8")) == payload


def test_loads_rejects_garbage_and_non_objects() -> None:
    assert _loads("not json") is None
    assert _loads("[1,2,3]") is None
    assert _loads(None) is None


# ---------------------------------------------------------------- 事件处理


def test_ready_stores_session_id() -> None:
    logs: list[str] = []
    options = GatewayOptions(on_log=logs.append)
    state = _SessionState()

    action = run(
        _handle(
            {"op": GatewayOp.DISPATCH, "t": "READY", "d": {"session_id": "s-1"}}, options, state
        )
    )

    assert action == "ok"
    assert state.session_id == "s-1"
    assert any("READY" in line for line in logs)


def test_message_event_reaches_handler() -> None:
    received: list[InboundMessage] = []
    options = GatewayOptions(handler=received.append)
    state = _SessionState()
    payload = {
        "op": GatewayOp.DISPATCH,
        "t": "C2C_MESSAGE_CREATE",
        "s": 7,
        "d": {"id": "m-1", "content": "/status", "author": {"user_openid": "u-1"}},
    }

    action = run(_handle(payload, options, state))

    assert action == "ok"
    assert len(received) == 1
    assert received[0].sender_id == "u-1"
    assert state.events == 1


def test_handler_exception_does_not_break_the_gateway() -> None:
    def boom(_message: InboundMessage) -> None:
        raise RuntimeError("handler 挂了")

    logs: list[str] = []
    options = GatewayOptions(handler=boom, on_log=logs.append)
    payload = {
        "op": GatewayOp.DISPATCH,
        "t": "C2C_MESSAGE_CREATE",
        "d": {"id": "m-1", "content": "x", "author": {"user_openid": "u-1"}},
    }

    assert run(_handle(payload, options, _SessionState())) == "ok"
    assert any("处理消息失败" in line for line in logs)


def test_unknown_event_is_ignored() -> None:
    options = GatewayOptions()
    assert (
        run(
            _handle(
                {"op": GatewayOp.DISPATCH, "t": "GUILD_CREATE", "d": {}}, options, _SessionState()
            )
        )
        == "ok"
    )


def test_reconnect_op_asks_for_reconnect() -> None:
    assert (
        run(_handle({"op": GatewayOp.RECONNECT}, GatewayOptions(), _SessionState())) == "reconnect"
    )


def test_invalid_session_clears_state() -> None:
    state = _SessionState(session_id="s-1", seq=42)
    options = GatewayOptions()
    assert run(_handle({"op": GatewayOp.INVALID_SESSION}, options, state)) == "reconnect"
    assert state.session_id == ""
    assert state.seq is None


def test_heartbeat_ack_is_noop() -> None:
    assert run(_handle({"op": GatewayOp.HEARTBEAT_ACK}, GatewayOptions(), _SessionState())) == "ok"


def test_stopped_flag_from_should_stop() -> None:
    flag = {"stop": False}
    options = GatewayOptions(should_stop=lambda: flag["stop"])
    assert options.stopped() is False
    flag["stop"] = True
    assert options.stopped() is True
