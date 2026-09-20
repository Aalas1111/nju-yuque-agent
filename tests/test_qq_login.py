"""扫码登录流程测试：成功 / 过期刷新 / 取消 / 超时 / 连续报错 / 两阶段接口。"""

from __future__ import annotations

import threading

import pytest

from tests.qq_fakes import FakeProtocol, completed, expired, pending
from yuque_agent.qqbot.login import QrLoginFlow, QrLoginManager
from yuque_agent.qqbot.protocol import QQBotError
from yuque_agent.qqbot.qr import has_png_support


def fake_clock(step: float = 1.0):
    """假时钟：每读一次前进 ``step`` 秒，于是「超时」不用真的等。"""
    state = {"now": 0.0}

    def clock() -> float:
        state["now"] += step
        return state["now"]

    return clock


def make_flow(protocol: FakeProtocol, **kwargs) -> QrLoginFlow:
    """默认把 sleep 换成空操作、时钟换成假时钟，只测分支逻辑。"""
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("clock", fake_clock())
    kwargs.setdefault("qr_timeout", 60.0)
    return QrLoginFlow(protocol, source="yuque-agent", **kwargs)


# ---------------------------------------------------------------- 单次流程


def test_login_succeeds_on_first_scan() -> None:
    protocol = FakeProtocol(polls=[pending(), completed(app_id="102000001", secret="sek")])
    seen: list[tuple[str, int, str | None]] = []
    flow = make_flow(protocol, on_qr=lambda url, attempt, data: seen.append((url, attempt, data)))

    result = flow.run()

    assert result.connected is True
    assert result.app_id == "102000001"
    assert result.app_secret == "sek"
    assert result.user_openid == "u-1"
    assert result.refreshes == 0
    assert "task_id=task-0" in result.qr_url
    assert seen and seen[0][1] == 1
    assert seen[0][2] is None  # with_data_url 默认关，不去渲染 PNG


def test_login_reports_qr_data_url_when_asked() -> None:
    if not has_png_support():
        pytest.skip("没装 Pillow，data URL 不可用")
    protocol = FakeProtocol(polls=[completed()])
    seen: list[str | None] = []
    flow = make_flow(protocol, with_data_url=True, on_qr=lambda _u, _a, data: seen.append(data))
    result = flow.run()
    assert result.connected
    assert seen and (seen[0] or "").startswith("data:image/png;base64,")


def test_login_refreshes_after_expiry() -> None:
    protocol = FakeProtocol(polls=[expired(), completed(app_id="app-2")])
    statuses: list[str] = []
    flow = make_flow(protocol, on_status=statuses.append, max_refreshes=3)

    result = flow.run()

    assert result.connected is True
    assert result.refreshes == 1
    assert protocol.created == ["task-0", "task-1"]
    assert any("过期" in text for text in statuses)
    assert "task_id=task-1" in result.qr_url


def test_login_times_out_and_gives_up_after_max_refreshes() -> None:
    protocol = FakeProtocol(default_status=int(pending().status))
    statuses: list[str] = []
    # qr_timeout=0 → 内层循环立刻走「超时」分支，不会 poll
    flow = make_flow(protocol, qr_timeout=0.0, max_refreshes=2, on_status=statuses.append)

    result = flow.run()

    assert result.connected is False
    assert "放弃" in result.message
    assert len(protocol.created) == 3  # 首次 + 2 次刷新
    assert protocol.poll_count == 0
    assert any("超时" in text for text in statuses)


def test_login_retries_poll_errors_then_succeeds() -> None:
    protocol = FakeProtocol(
        polls=[QQBotError("网络抖了"), QQBotError("又抖了"), completed(secret="fine")]
    )
    statuses: list[str] = []
    flow = make_flow(protocol, on_status=statuses.append, max_errors=3)

    result = flow.run()

    assert result.connected is True
    assert result.app_secret == "fine"
    assert sum("轮询失败" in text for text in statuses) == 2


def test_login_gives_up_after_too_many_poll_errors() -> None:
    protocol = FakeProtocol(polls=[QQBotError("boom")] * 10)
    flow = make_flow(protocol, max_errors=2)
    result = flow.run()
    assert result.connected is False
    assert "已重试 2 次" in result.message


def test_login_retries_create_task_errors() -> None:
    protocol = FakeProtocol(polls=[completed()], create_errors=1)
    statuses: list[str] = []
    flow = make_flow(protocol, on_status=statuses.append)
    result = flow.run()
    assert result.connected is True
    assert any("创建绑定任务失败" in text for text in statuses)


def test_login_aborts_when_cancelled() -> None:
    protocol = FakeProtocol(polls=[pending()])
    cancel = threading.Event()
    cancel.set()
    flow = make_flow(protocol, cancel=cancel)
    result = flow.run()
    assert result.connected is False
    assert "已取消" in result.message
    assert protocol.created == []


def test_login_cancel_midway_stops(polling: int = 0) -> None:
    protocol = FakeProtocol(polls=[pending()] * 100)
    cancel = threading.Event()
    flow = make_flow(protocol, cancel=cancel, on_status=lambda _t: cancel.set())
    result = flow.run()
    assert result.connected is False
    assert "已取消" in result.message


def test_login_rejects_missing_app_id() -> None:
    from tests.qq_fakes import BindResult

    protocol = FakeProtocol(
        polls=[
            BindResult(
                status=2,
                bot_app_id="",
                bot_encrypt_secret=completed().bot_encrypt_secret,
            )
        ]
    )
    result = make_flow(protocol).run()
    assert result.connected is False
    assert "AppID" in result.message


def test_login_result_credentials_shape_matches_reference() -> None:
    protocol = FakeProtocol(polls=[completed(app_id="app", secret="sec", openid="openid")])
    result = make_flow(protocol).run()
    assert result.credentials == [{"appId": "app", "appSecret": "sec", "userOpenid": "openid"}]
    payload = result.to_dict()
    assert payload["connected"] is True
    assert payload["credentials"][0]["appId"] == "app"


# ---------------------------------------------------------------- 两阶段


def test_manager_start_wait_and_save_callback() -> None:
    protocol = FakeProtocol(polls=[completed(app_id="app-m", secret="sec-m", openid="u-m")])
    saved: list[str] = []
    manager = QrLoginManager(
        protocol,
        with_data_url=False,
        on_connected=lambda result: saved.append(result.app_id),
        flow_kwargs={"sleep": lambda _s: None, "clock": fake_clock(), "qr_timeout": 60.0},
    )
    started = manager.start()
    # 假时钟 + 空 sleep 下后台线程可能已经把码扫完了，所以允许 connected
    assert started["state"] in ("starting", "waiting", "connected")
    assert started["qrUrl"]

    payload = manager.wait(timeout=10)
    assert payload["connected"] is True
    assert payload["credentials"][0]["appSecret"] == "sec-m"
    assert saved == ["app-m"]
    # 会话被消费掉：只能 wait 一次
    assert manager.wait(timeout=0.1)["connected"] is False


def test_manager_status_reports_waiting() -> None:
    protocol = FakeProtocol(polls=[pending()] * 100)
    manager = QrLoginManager(
        protocol,
        with_data_url=False,
        flow_kwargs={"sleep": lambda _s: None, "clock": lambda: 0.0, "qr_timeout": 60.0},
    )
    manager.start()
    status = manager.status()
    assert status["state"] in ("waiting", "starting")
    assert status["connected"] is False
    assert status["qrUrl"]
    manager.close()


def test_manager_cancel() -> None:
    protocol = FakeProtocol(polls=[pending()] * 100)
    manager = QrLoginManager(
        protocol,
        with_data_url=False,
        flow_kwargs={"sleep": lambda _s: None, "clock": lambda: 0.0, "qr_timeout": 60.0},
    )
    manager.start()
    assert manager.cancel()["cancelled"] is True
    assert manager.cancel()["cancelled"] is False
    manager.close()


def test_manager_wait_without_session() -> None:
    manager = QrLoginManager(FakeProtocol())
    payload = manager.wait(timeout=0.1)
    assert payload["connected"] is False
    assert "没有正在进行的登录会话" in payload["message"]


def test_manager_start_replaces_previous_session() -> None:
    protocol = FakeProtocol(polls=[pending()] * 100)
    manager = QrLoginManager(
        protocol,
        with_data_url=False,
        flow_kwargs={"sleep": lambda _s: None, "clock": lambda: 0.0, "qr_timeout": 60.0},
    )
    manager.start()
    manager.start()  # 前一个会话被取消
    assert manager.status()["state"] in ("waiting", "starting")
    manager.close()
