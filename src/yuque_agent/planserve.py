"""带密钥的 ``plan.json`` 下载口。

**为什么需要它**：下游（cac）跑的是浏览器里的油猴脚本，**没法直连服务器** ——
它没有任何服务器凭证，也不能开 SSH。所以取件只能靠人工。取件通道有两条：

* ``scp``（见 `docs/handoff.md`）——零新增攻击面，但要敲命令；
* **这个 HTTP 口** —— 打开网页、粘密钥、点下载。

**它是哪个级别的东西**（说清楚，免得被当成安全边界）：
服务器没有域名，所以只有明文 HTTP，密钥和申请内容都在网上裸奔。
项目负责人明确接受了这个风险（活动信息不算机密信息）。
所以这里的目标**不是**「防住有心人」，而是三件做得到的事：

1. **不给路过的扫描器送文件** —— 没密钥拿不到；
2. **路径不可越狱** —— 只能取那几个由程序自己算出来的固定文件，
   URL 里的 ``cycle`` 也过正则校验，绝不拼接用户输入当路径；
3. **尝试要能被看见** —— 请求进 journald（**但不记密钥**），
   失败次数超限就锁一小会儿。

没配 ``YQA_PLAN_KEY`` 时 :func:`serve` **直接拒绝启动**（失败关闭）——
宁可不开张，也不能出现「没密钥也能下」的状态。
"""

from __future__ import annotations

import hmac
import json
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import Settings
from .week import is_cycle_title

#: 只认这一条归档路径的形状；``cycle`` 另外还要过 :func:`is_cycle_title`。
_ARCHIVE_RE = re.compile(r"^/archive/([^/]{1,32})/plan\.json$")

#: 同一个 IP 在 ``_WINDOW`` 秒内失败这么多次就锁住。
_MAX_FAILURES = 8
_WINDOW = 300.0

_FORM_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>申请清单下载</title></head>
<body style="font-family:system-ui,-apple-system,sans-serif;max-width:34rem;margin:4rem auto;padding:0 1rem">
<h1 style="font-size:1.3rem">申请清单下载</h1>
<p>输入密钥，然后下载**当前周期**的 <code>plan.json</code>。</p>
<form method="get" action="/plan.json">
  <p><input name="key" type="password" placeholder="密钥" autofocus
            style="width:100%;padding:.6rem;font-size:1rem;box-sizing:border-box"></p>
  <p><button type="submit" style="padding:.6rem 1.2rem;font-size:1rem">下载</button></p>
</form>
<p style="color:#666;font-size:.9rem">
  下载后先 <code>crb plan --file plan.json</code> 看一眼方案，确认无误再 <code>--save</code>。<br>
  想取往期的：<code>/archive/&lt;周期&gt;/plan.json?key=...</code>（如 0919-0925）。
</p>
</body></html>
"""


class _State:
    """失败计数（内存里就够——重启后重新计，而且这只挡暴力猜）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}

    def locked(self, who: str) -> bool:
        with self._lock:
            hits = [t for t in self._fails.get(who, []) if time.monotonic() - t < _WINDOW]
            self._fails[who] = hits
            return len(hits) >= _MAX_FAILURES

    def record_failure(self, who: str) -> None:
        with self._lock:
            self._fails.setdefault(who, []).append(time.monotonic())

    def clear(self, who: str) -> None:
        with self._lock:
            self._fails.pop(who, None)


class PlanHandler(BaseHTTPRequestHandler):
    server_version = "yuque-agent-plan/1.0"
    protocol_version = "HTTP/1.1"

    # 由 :func:`serve` 注入
    settings: Settings
    state: _State

    # -- 路由 -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        who = self.client_address[0]

        if path == "/healthz":
            # 不带密钥也不泄任何内容，纯粹给监控用
            return self._send_text(HTTPStatus.OK, "ok\n")

        if path == "/":
            return self._send_html(HTTPStatus.OK, _FORM_HTML)

        if path == "/plan.json":
            return self._serve(self.settings.plan_file, parsed.query, who)

        match = _ARCHIVE_RE.match(path)
        if match and is_cycle_title(match.group(1)):
            target = self.settings.cycle_archive_dir(match.group(1)) / "plan.json"
            return self._serve(target, parsed.query, who)

        return self._send_text(HTTPStatus.NOT_FOUND, "没有这个地址\n")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    # -- 取件 -------------------------------------------------------------
    def _key_from(self, query: str) -> str:
        from_header = self.headers.get("X-Plan-Key") or ""
        if from_header:
            return from_header
        return (parse_qs(query).get("key") or [""])[0]

    def _serve(self, path: Path, query: str, who: str) -> None:
        if self.state.locked(who):
            self._log("locked", path.name)
            return self._send_text(HTTPStatus.TOO_MANY_REQUESTS, "尝试次数太多，请等几分钟再来\n")

        expected = self.settings.plan_key
        got = self._key_from(query)
        # 恒定时间比较：不让「前缀对不对」从耗时里漏出去
        if not got or not hmac.compare_digest(got, expected):
            self.state.record_failure(who)
            self._log("denied", path.name)
            return self._send_text(HTTPStatus.UNAUTHORIZED, "密钥不对\n")

        self.state.clear(who)
        if not path.is_file():
            self._log("empty", path.name)
            return self._send_text(HTTPStatus.NOT_FOUND, "还没有清单（这一轮还没有申请）\n")

        body = path.read_bytes()
        self._log("served", f"{path.name} {len(body)}B")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 一律不要缓存：cac 拿到的必须是**此刻**那一份，
        # 否则他会提交一个上周的清单，而这里谁都看不出来。
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Content-Disposition", 'attachment; filename="plan.json"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- 输出 -------------------------------------------------------------
    def _send_text(self, status: HTTPStatus, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_html(self, status: HTTPStatus, html: str) -> None:
        body = html.replace("**", "").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _log(self, event: str, detail: str) -> None:
        """写进 stderr → journald。**只记事件与文件名，绝不记密钥。**"""
        print(f"[plan] {event} from={self.client_address[0]} {detail}", flush=True)

    def log_message(self, fmt: str, *args: Any) -> None:
        # 关掉默认日志：它会把带 ?key=... 的完整 URL 打进日志里。
        pass


class PlanServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(settings: Settings, *, host: str = "0.0.0.0") -> PlanServer:
    """造一个下载口。**没配密钥就抛异常**（失败关闭）。"""
    if not settings.plan_key:
        raise RuntimeError(
            "没配 YQA_PLAN_KEY —— 拒绝启动。\n"
            "这个口没有域名、只有明文 HTTP，再不加密钥就等于把申请清单公开挂出去。"
        )
    handler = type(
        "_BoundPlanHandler",
        (PlanHandler,),
        {
            "settings": settings,
            "state": _State(),
        },
    )
    return PlanServer((host, settings.plan_port), handler)


def serve(settings: Settings, *, host: str = "0.0.0.0") -> None:
    server = build_server(settings, host=host)
    print(
        f"[plan] 下载口已开：http://{host}:{settings.plan_port}/  "
        f"（周期 {settings.repo}，只放行 outbox/plan.json 与 archive/<周期>/plan.json）",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def read_plan(settings: Settings) -> dict[str, Any] | None:
    """本地读一下当前交付件（给 CLI/doctor 用，不走 HTTP）。"""
    try:
        return json.loads(settings.plan_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
