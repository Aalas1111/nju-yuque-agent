"""把扫码登录暴露成**本地 HTTP 接口**——一个后端/网页可以直接调的「接口」。

形状刻意对齐参考实现 ``qqbot_backend_sdk`` 的两阶段接口：

======================================  ==========================================
参考实现（Python 函数）                    本模块（HTTP）
======================================  ==========================================
``start_qr_login(account_id, source)``    ``POST /qr/start``
``wait_qr_login(account_id)``             ``GET  /qr/wait?timeout=120``
``cancel_qr_login(account_id)``           ``POST /qr/cancel``
——                                        ``GET  /qr/status``（多了个非阻塞查询）
——                                        ``GET  /``（手机上直接扫的页面）
——                                        ``GET  /qr.png``（二维码原图）
======================================  ==========================================

安全性：**默认只绑 127.0.0.1**。要绑非回环地址必须同时给 ``--allow-remote`` 与
``--http-token``——否则任何能访问这个端口的人都能把机器人的 AppSecret 拿走
（``/qr/wait`` 默认**不返回密钥**，密钥直接写进本地凭证文件；确实需要返回时用 ``expose_secret=True``）。
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .login import QrLoginManager
from .protocol import QQBotError
from .qr import qr_png_bytes

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}

_HTML_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>yuque-agent · QQBot 扫码绑定</title>
<style>
 body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0;
        min-height: 100vh; display: flex; align-items: center; justify-content: center;
        background: #0f1115; color: #e8eaed; }}
 .card {{ background: #171a21; padding: 32px 36px; border-radius: 16px; text-align: center;
          box-shadow: 0 12px 40px rgba(0,0,0,.45); max-width: 420px; }}
 h1 {{ font-size: 18px; font-weight: 600; margin: 0 0 6px; }}
 .state {{ font-size: 13px; color: #9aa0a6; margin-bottom: 18px; }}
 img {{ width: 260px; height: 260px; image-rendering: pixelated; background: #fff;
        padding: 10px; border-radius: 10px; }}
 .msg {{ margin-top: 16px; font-size: 14px; line-height: 1.6; }}
 .ok {{ color: #34d399; }} .bad {{ color: #f87171; }}
 code {{ background: #22262f; padding: 2px 6px; border-radius: 6px; font-size: 12px; }}
</style>
<meta http-equiv="refresh" content="3"></head>
<body><div class="card">
  <h1>用手机 QQ 扫码绑定机器人</h1>
  <div class="state">账户 {account} · 状态 {state} · 第 {refreshes_note} 张二维码</div>
  {qr_block}
  <div class="msg {klass}">{message}</div>
  <div class="msg"><code>{url}</code></div>
</div></body></html>
"""


class LoginHttpServer:
    """扫码登录的 HTTP 门面。``serve_forever`` 阻塞，``shutdown`` 可被其他线程调用。"""

    def __init__(
        self,
        manager: QrLoginManager,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        token: str = "",
        allow_remote: bool = False,
        expose_secret: bool = False,
        log: Callable[[str], None] | None = None,
        shutdown_event: threading.Event | None = None,
    ) -> None:
        self.manager = manager
        self.host = host
        self.port = port
        self.token = token
        self.allow_remote = allow_remote
        self.expose_secret = expose_secret
        self.log = log
        self.shutdown_event = shutdown_event or threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._guard()

    # -- 校验 -------------------------------------------------------------
    def _guard(self) -> None:
        if self.host not in LOOPBACK_HOSTS:
            if not self.allow_remote:
                raise QQBotError(
                    f"拒绝绑定非回环地址 {self.host!r}：加 --allow-remote 明确表示你知道风险"
                )
            if not self.token:
                raise QQBotError(
                    "绑定到非回环地址时必须给 --http-token：否则同网段任何人都能拿走 AppSecret"
                )

    @property
    def url(self) -> str:
        host = self.host if self.host not in ("0.0.0.0", "::") else "127.0.0.1"
        return f"http://{host}:{self.port}/"

    # -- 生命周期 ---------------------------------------------------------
    def serve_forever(self) -> None:
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._log(f"[qqbot:qr] 扫码登录接口已启动：{self.url}")
        self._log(f"[qqbot:qr] 用手机浏览器打开 {self.url}，或让前端调 POST /qr/start")
        threading.Thread(target=self._watch_shutdown, daemon=True).start()
        try:
            self._server.serve_forever(poll_interval=0.5)
        finally:
            self._server.server_close()
            self.manager.close()
            self._log("[qqbot:qr] 接口已停止")

    def _watch_shutdown(self) -> None:
        """``--exit-after-login`` 时：绑成功就自己关掉。"""
        self.shutdown_event.wait()
        self.shutdown()

    def shutdown(self) -> None:
        if self._server is not None:
            threading.Thread(target=self._server.shutdown, daemon=True).start()

    def _log(self, text: str) -> None:
        if self.log is not None:
            self.log(text)


# ---------------------------------------------------------------- 路由


def _make_handler(server: LoginHttpServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "yuque-agent-qqbot-qr"

        # -- 工具 ---------------------------------------------------------
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - 覆盖基类
            if server.log is not None:
                server.log(f"[qqbot:qr] {self.address_string()} {fmt % args}")

        def _authorized(self) -> bool:
            if not server.token:
                return True
            header = self.headers.get("X-Auth-Token", "")
            if header == server.token:
                return True
            query = urllib.parse.urlparse(self.path).query
            return urllib.parse.parse_qs(query).get("token", [""])[0] == server.token

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str, status: int = 200) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_png(self, payload: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        # -- GET ----------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 - 基类约定
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"
            if not self._authorized():
                self._send_json({"error": "unauthorized"}, status=401)
                return
            if route == "/":
                self._render_page()
                return
            if route == "/health":
                self._send_json({"ok": True, "service": "yuque-agent qqbot qr login"})
                return
            if route == "/qr/status":
                self._send_json(server.manager.status(include_secret=False))
                return
            if route == "/qr/wait":
                params = urllib.parse.parse_qs(parsed.query)
                raw_timeout = params.get("timeout", [""])[0]
                try:
                    timeout = float(raw_timeout) if raw_timeout else None
                except ValueError:
                    timeout = None
                payload = server.manager.wait(timeout=timeout, include_secret=server.expose_secret)
                self._send_json(payload)
                return
            if route == "/qr.png":
                status = server.manager.status()
                url = status.get("qrUrl") or ""
                payload = qr_png_bytes(url) if url else None
                if payload is None:
                    self._send_json({"error": "还没有二维码，先 POST /qr/start"}, status=404)
                    return
                self._send_png(payload)
                return
            self._send_json({"error": "not found", "path": route}, status=404)

        # -- POST ---------------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802 - 基类约定
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path.rstrip("/") or "/"
            if not self._authorized():
                self._send_json({"error": "unauthorized"}, status=401)
                return
            if route == "/qr/start":
                payload = server.manager.start()
                self._send_json(
                    {
                        "qrDataUrl": payload.get("qrDataUrl"),
                        "qrUrl": payload.get("qrUrl"),
                        "state": payload.get("state"),
                        "account": payload.get("account"),
                        "message": payload.get("message") or "请使用手机 QQ 扫描二维码完成绑定",
                    }
                )
                return
            if route == "/qr/cancel":
                self._send_json(server.manager.cancel())
                return
            self._send_json({"error": "not found", "path": route}, status=404)

        # -- 页面 ---------------------------------------------------------
        def _render_page(self) -> None:
            status = server.manager.status()
            state = str(status.get("state") or "idle")
            if state == "idle":
                # 打开页面就顺手开一个会话，省得用户再点一次
                status = server.manager.start()
                state = str(status.get("state") or "waiting")
            url = str(status.get("qrUrl") or "")
            data_url = status.get("qrDataUrl")
            if data_url:
                qr_block = f'<img src="{data_url}" alt="扫码绑定二维码">'
            elif url:
                png = qr_png_bytes(url)
                if png is not None:
                    encoded = base64.b64encode(png).decode("ascii")
                    qr_block = f'<img src="data:image/png;base64,{encoded}" alt="扫码绑定二维码">'
                else:
                    qr_block = '<div class="msg">没装 qrcode/Pillow，画不出二维码，请复制下面的链接到手机打开。</div>'
            else:
                qr_block = '<div class="msg">正在生成二维码…</div>'
            message = str(status.get("message") or "")
            klass = (
                "ok"
                if state == "connected"
                else ("bad" if state in ("failed", "cancelled") else "")
            )
            refreshes = int(status.get("refreshes") or 0) + 1
            self._send_html(
                _HTML_PAGE.format(
                    account=status.get("account") or "default",
                    state=state,
                    refreshes_note=refreshes,
                    qr_block=qr_block,
                    message=message,
                    klass=klass,
                    url=url or "(等待二维码)",
                )
            )

    return Handler


def serve_login_http(
    manager: QrLoginManager,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str = "",
    allow_remote: bool = False,
    expose_secret: bool = False,
    log: Callable[[str], None] | None = None,
    shutdown_event: threading.Event | None = None,
) -> LoginHttpServer:
    """建好并启动（阻塞）。返回的 server 已经跑完生命周期。"""
    server = LoginHttpServer(
        manager,
        host=host,
        port=port,
        token=token,
        allow_remote=allow_remote,
        expose_secret=expose_secret,
        log=log,
        shutdown_event=shutdown_event,
    )
    server.serve_forever()
    return server


__all__ = ["LOOPBACK_HOSTS", "LoginHttpServer", "serve_login_http"]
