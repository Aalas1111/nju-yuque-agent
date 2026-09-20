"""扫码登录的**本地 HTTP 接口**端到端测试。

真的起一个 socket、真的发 HTTP 请求，但协议层是假的（不出网）。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.qq_fakes import FakeProtocol, completed, pending
from yuque_agent.qqbot.login import QrLoginManager
from yuque_agent.qqbot.login_http import LoginHttpServer
from yuque_agent.qqbot.protocol import QQBotError


def http_get(url: str, token: str = "") -> tuple[int, dict | str]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("X-Auth-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - 本地测试
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    return status, _maybe_json(body)


def http_post(url: str) -> tuple[int, dict | str]:
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - 本地测试
            return response.status, _maybe_json(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _maybe_json(body: str):
    try:
        return json.loads(body)
    except ValueError:
        return body


class ServerFixture:
    def __init__(self, tmp_path: Path, *, gated: bool = False, **server_kwargs) -> None:
        self.protocol = FakeProtocol(
            polls=[pending(), completed(app_id="app-http", secret="sec-http")]
        )
        self.gate = threading.Event()
        if gated:
            # 让第一次 poll 卡住，测试才能稳定地观察到「还在等扫码」这个状态
            original = self.protocol.poll_bind_result

            def gated_poll(task_id: str):
                self.gate.wait(timeout=5)
                return original(task_id)

            self.protocol.poll_bind_result = gated_poll  # type: ignore[method-assign]
        self.saved: list[str] = []
        self.shutdown = threading.Event()
        self.manager = QrLoginManager(
            self.protocol,
            with_data_url=False,
            on_connected=lambda result: self.saved.append(result.app_id),
            flow_kwargs={
                "sleep": lambda _s: None,
                "clock": lambda: 0.0,
                "qr_timeout": 60.0,
                "poll_interval": 0.01,
            },
        )
        self.server = LoginHttpServer(
            self.manager,
            host="127.0.0.1",
            port=0,
            log=None,
            shutdown_event=self.shutdown,
            **server_kwargs,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> ServerFixture:
        self.thread.start()
        deadline = time.monotonic() + 5
        while self.server.port == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert self.server.port != 0, "HTTP 服务没起来"
        self.base = f"http://127.0.0.1:{self.server.port}"
        return self

    def __exit__(self, *_exc) -> None:
        self.shutdown.set()
        self.server.shutdown()
        self.thread.join(timeout=5)


def test_health_endpoint(tmp_path: Path) -> None:
    with ServerFixture(tmp_path) as fixture:
        status, payload = http_get(f"{fixture.base}/health")
    assert status == 200
    assert payload["ok"] is True


def test_full_start_status_wait_flow(tmp_path: Path) -> None:
    with ServerFixture(tmp_path, gated=True) as fixture:
        status, started = http_post(f"{fixture.base}/qr/start")
        assert status == 200
        assert started["qrUrl"].startswith("https://q.qq.com/")

        status, snapshot = http_get(f"{fixture.base}/qr/status")
        assert status == 200
        assert snapshot["connected"] is False
        assert snapshot["state"] in ("starting", "waiting")

        fixture.gate.set()  # 放行 → 第一次 poll 说 PENDING，第二次说 COMPLETED
        status, waited = http_get(f"{fixture.base}/qr/wait?timeout=5")
        assert status == 200
        assert waited["connected"] is True
        assert waited["appId"] == "app-http"
        # 默认不通过 HTTP 吐密钥
        assert "credentials" not in waited
    assert fixture.saved == ["app-http"]


def test_wait_can_expose_secret_when_asked(tmp_path: Path) -> None:
    with ServerFixture(tmp_path, expose_secret=True) as fixture:
        http_post(f"{fixture.base}/qr/start")
        status, waited = http_get(f"{fixture.base}/qr/wait?timeout=5")
    assert status == 200
    assert waited["credentials"][0]["appSecret"] == "sec-http"


def test_cancel_endpoint(tmp_path: Path) -> None:
    with ServerFixture(tmp_path) as fixture:
        http_post(f"{fixture.base}/qr/start")
        status, payload = http_post(f"{fixture.base}/qr/cancel")
        assert status == 200
        assert payload["cancelled"] is True


def test_page_renders_html_with_qr(tmp_path: Path) -> None:
    with ServerFixture(tmp_path) as fixture:
        status, body = http_get(f"{fixture.base}/")
    assert status == 200
    assert isinstance(body, str)
    assert "扫码绑定" in body
    assert "data:image/png;base64," in body or "connect.html" in body


def test_qr_png_endpoint(tmp_path: Path) -> None:
    with ServerFixture(tmp_path) as fixture:
        http_post(f"{fixture.base}/qr/start")
        status, body = http_get(f"{fixture.base}/qr.png")
    if status == 404:  # 没装 Pillow
        pytest.skip("没装 Pillow，出不了 PNG")
    assert status == 200
    assert isinstance(body, str)  # PNG 不是 JSON，这里只确认有内容


def test_unknown_route_is_404(tmp_path: Path) -> None:
    with ServerFixture(tmp_path) as fixture:
        status, _payload = http_get(f"{fixture.base}/nope")
    assert status == 404


def test_token_is_required_when_configured(tmp_path: Path) -> None:
    with ServerFixture(tmp_path, token="s3cret-token") as fixture:
        status, _payload = http_post(f"{fixture.base}/qr/start")
        assert status == 401
        status, _payload = http_get(f"{fixture.base}/health?token=s3cret-token")
        assert status == 200


def test_refuses_non_loopback_without_flag() -> None:
    manager = QrLoginManager(FakeProtocol(), with_data_url=False)
    with pytest.raises(QQBotError, match="allow-remote|拒绝绑定"):
        LoginHttpServer(manager, host="0.0.0.0", port=0)


def test_refuses_remote_without_token() -> None:
    manager = QrLoginManager(FakeProtocol(), with_data_url=False)
    with pytest.raises(QQBotError, match="http-token"):
        LoginHttpServer(manager, host="0.0.0.0", port=0, allow_remote=True)
