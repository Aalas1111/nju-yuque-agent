"""``plan.json`` 下载口。**没有密钥，打开即下载。**

## 为什么不再要密钥

原来的设计是「输对密钥才能下」。项目负责人和 cac 的判断是：**密钥保不住**——
服务器没有域名，只有明文 HTTP，密钥在 URL 里、在浏览器历史里、在截图里都会漏；
与其维持一个「看起来有防护、实际拦不住人」的假象，不如干脆不做防护，
把「访问即下载」做成一个清清楚楚的事实。

**它是哪个级别的东西（别把它当安全边界）**：
这是一个**公开**的 HTTP 端点，只放行那几个由程序自己算出来的固定文件。
任何知道地址的人都能拿到当前周期的申请清单。

能做到的（都有测试）：

1. **路径不可越狱** —— 只能取 `outbox/plan.json` 与
   `outbox/archive/<周期>/plan.json`；URL 里的周期还要过格式校验；
2. **`plan.defaults.json` 永远取不到** —— 那里面是**借用人姓名与手机号**，
   比申请清单敏感得多，而且 cac 不需要它（`defaults` 已内联在 `plan.json` 里）；
3. **⚠️ `defaults` 是唯一的 PII 风险** —— 一旦有人往 `plan.defaults.json`
   里填了真名/手机号，它会跟着 `plan.json` 一起公开。所以启动时会检查并**大声告警**，
   `yqa doctor` 里也有一行。今天它是空的，所以当前只暴露活动信息。

## 路由

```text
GET /                            一个网页：显示当前周期/条数/更新时间 + 下载按钮
GET /download                    直接下载 plan.json（cac 说的「访问 download 立刻下载」）
GET /plan.json                   同上（别名，方便 curl / 脚本）
GET /archive/<周期>/plan.json      往期清单（那个版本是冻结的）
GET /healthz                     给监控用，不泄任何内容
```
"""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import Settings
from .week import is_cycle_title

#: 只认这一条归档路径的形状；``cycle`` 另外还要过 :func:`is_cycle_title`。
_ARCHIVE_RE = re.compile(r"^/archive/([^/]{1,32})/plan\.json$")

#: 下载时会用到的文件名（写进 Content-Disposition，方便 cac 直接存成 plan.json）。
_DOWNLOAD_NAME = "plan.json"


def _page(settings: Settings) -> str:
    """给人看的下载页：一眼看出「这是哪一周、几条、什么时候更新的」。"""
    plan = read_plan(settings)
    if plan is None:
        summary = "<p style='color:#b45309'>当前还没有清单（这一轮还没有申请）。</p>"
        rows = ""
    else:
        activities = plan.get("activities") or []
        dates = sorted({str(a.get("date") or "") for a in activities if a.get("date")})
        span = f"{dates[0]} ~ {dates[-1]}" if dates else "（无）"
        summary = (
            f"<p><b>周期 {plan.get('cycle') or '?'}</b> · "
            f"{len(activities)} 条 · 活动日期 {span}<br>"
            f"<span style='color:#666;font-size:.9rem'>更新于 "
            f"{plan.get('generated_at') or '?'}</span></p>"
        )
        rows = "".join(
            f"<tr><td>{a.get('date') or ''}</td><td>{a.get('period') or ''}</td>"
            f"<td>{a.get('title') or ''}</td><td>{a.get('campus') or ''}</td>"
            f"<td>{a.get('building') or '不限'}</td></tr>"
            for a in activities
        )
    table = (
        "<table style='border-collapse:collapse;width:100%;font-size:.9rem'>"
        "<tr style='text-align:left;color:#666'><th>日期</th><th>节次</th>"
        "<th>活动</th><th>校区</th><th>楼</th></tr>" + rows + "</table>"
        if rows
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>教室借用申请清单</title></head>
<body style="font-family:system-ui,-apple-system,sans-serif;max-width:44rem;margin:3rem auto;padding:0 1rem">
<h1 style="font-size:1.25rem">教室借用申请清单</h1>
{summary}
<p><a href="/download"
      style="display:inline-block;padding:.7rem 1.4rem;background:#1d4ed8;color:#fff;
             text-decoration:none;border-radius:.4rem;font-size:1rem">下载 plan.json</a></p>
{table}
<p style="color:#666;font-size:.85rem;margin-top:2rem">
  下载后先 <code>crb plan --file plan.json</code> 看一眼方案，确认无误再 <code>--save</code>。<br>
  <b>下载前请对一眼上面的周期号</b>——那能看出你有没有拿到上一周那份。<br>
  往期：<code>/archive/&lt;周期&gt;/plan.json</code>（如 0919-0925）。
</p>
</body></html>
"""


class PlanHandler(BaseHTTPRequestHandler):
    server_version = "yuque-agent-plan/2.0"
    protocol_version = "HTTP/1.1"

    settings: Settings  # 由 build_server 注入

    # -- 路由 -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        path = urlsplit(self.path).path.rstrip("/") or "/"

        if path == "/healthz":
            return self._send(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
        if path == "/":
            return self._send(
                HTTPStatus.OK, _page(self.settings).encode("utf-8"), "text/html; charset=utf-8"
            )
        if path in ("/download", "/plan.json"):
            return self._serve(self.settings.plan_file)
        match = _ARCHIVE_RE.match(path)
        if match and is_cycle_title(match.group(1)):
            return self._serve(self.settings.cycle_archive_dir(match.group(1)) / "plan.json")
        return self._send(
            HTTPStatus.NOT_FOUND, "没有这个地址\n".encode(), "text/plain; charset=utf-8"
        )

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    # -- 取件 -------------------------------------------------------------
    def _serve(self, path: Path) -> None:
        # 目标路径**只由这里算出来**，从不拼用户输入，所以不存在路径越狱。
        if not path.is_file():
            self._log("empty", path.name)
            return self._send(
                HTTPStatus.NOT_FOUND,
                "还没有清单（这一周期还没有申请）\n".encode(),
                "text/plain; charset=utf-8",
            )
        body = path.read_bytes()
        self._log("served", f"{path.name} {len(body)}B")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 一律不要缓存：cac 拿到的必须是**此刻**那一份，
        # 否则他会提交一个上周的清单，而这里谁都看不出来。
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Content-Disposition", f'attachment; filename="{_DOWNLOAD_NAME}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _log(self, event: str, detail: str) -> None:
        """写进 stderr → journald。记的是「谁在什么时候取了什么」——省得事后猜。"""
        print(f"[plan] {event} from={self.client_address[0]} {detail}", flush=True)

    def log_message(self, fmt: str, *args: Any) -> None:
        # 关掉默认日志：格式不由我们控制，而且会多一行没用的话。
        pass


class PlanServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def pii_warning(settings: Settings) -> str:
    """``defaults`` 非空时给一句告警。

    保存借用人姓名/电话的那个文件就在下载口隔壁，而**下载口是公开的**
    （没有密钥，打开即下载）。`defaults` 被内联进 `plan.json`，所以一旦有人
    往里面填了真名和手机号，它们就会跟着公开。今天它是空的，所以只暴露活动信息
    ——但这件事必须**说出来**，不能等到哪天有人填了才由别人发现。
    """
    from .outputs import read_plan_defaults

    defaults = read_plan_defaults(settings)
    if not defaults:
        return ""
    keys = "、".join(sorted(defaults))
    return (
        f"⚠️ 下载口是**公开**的（无密钥），而 plan.defaults.json 里非空（{keys}）。\n"
        f"   这些字段会被内联进 plan.json 一起公开——如果里面有真名/手机号，"
        f"等于把它们挂到公网上。\n"
        f"   要么清空它（yqa export-plan 不传 --defaults 不会覆盖，需手动删文件），"
        f"要么确认这些信息可以公开。"
    )


def build_server(settings: Settings, *, host: str = "0.0.0.0") -> PlanServer:
    handler = type("_BoundPlanHandler", (PlanHandler,), {"settings": settings})
    return PlanServer((host, settings.plan_port), handler)


def serve(settings: Settings, *, host: str = "0.0.0.0") -> None:
    warning = pii_warning(settings)
    if warning:
        print(f"[plan] {warning}", flush=True)
    server = build_server(settings, host=host)
    print(
        f"[plan] 下载口已开：http://{host}:{settings.plan_port}/  "
        f"（**无密钥**，只放行 outbox/plan.json 与 archive/<周期>/plan.json；"
        f"repo {settings.repo}）",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def read_plan(settings: Settings) -> dict[str, Any] | None:
    """本地读一下当前交付件（给下载页 / CLI / doctor 用，不走 HTTP）。"""
    try:
        return json.loads(settings.plan_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
