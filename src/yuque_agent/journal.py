"""留痕层：session → markdown → 语雀《工作日志》文档。

这一层不属于 agent，属于**程序**。它存在的唯一理由是：

> 每次 agent 跑完，我得能看见「程序喂了它什么、它想了什么、它做了什么、它最后下了什么结论」。

所以渲染的原则是**不加工**：思考原样贴、工具参数与结果原样贴、错误原样贴。
渲染只负责排版，不负责美化——美化会让证据失真。

《工作日志》文档的结构：

```
# 工作日志
> 本文件由程序自动追加……
## 2026-09-21T00:05:12+08:00 · polling · accepted
…最新的一节（永远插在**第一节之前**）…
## 2026-09-20T…
…更早的…
```

**新节插在哪里是靠「结构」定位的，不是靠标记位。** 语雀读回来的是**规范化过的
markdown**，会把 HTML 剥掉（注释、`<details>` 都保留不下来）。实测踩到过：
写进去的 `<!-- NEWEST -->` 读回来就没了，于是每轮都把表头重贴一遍、把旧正文甩到
文末——**每跑一轮文末就多堆一份表头**。现在改为找「第一节的标题」（`## <时间戳>`），
这个在语雀的规范化里是能活下来的。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import Settings
from .session import read_events
from .yuque import YuqueClient, YuqueError

_SECTION_HEAD_RE = re.compile(r"^## \d{4}-\d{2}-\d{2}T", re.MULTILINE)

HEADER = (
    "# 工作日志\n"
    "\n"
    "> 本文件由程序自动追加，每次 agent 运行写一节，**最新的在最上面**。\n"
    "> 这些内容是从 session 原始记录渲染出来的，没有经过任何美化——"
    "包括 agent 的思考过程和每一次工具调用的原始参数与返回值。\n"
)

MAX_BLOCK_CHARS = 2500


def split_journal(body: str) -> tuple[str, str]:
    """把日志正文切成 ``(表头, 已有各节)``——新节就插在这两者之间。

    定位方式是找**第一节的标题**（形如 ``## 2026-09-21T08:20:38+08:00 · archive · …``）。
    找不到就说明还没有任何一节，整份都算表头。

    为什么不用一个标记位（如 ``<!-- NEWEST -->``）：语雀读回来的是**规范化过的 markdown**，
    HTML 会被剥掉。实测踩到过：写进去的标记读回来就没了，于是每一轮都走进
    「没有标记」的分支，把 HEADER 重贴一遍再把旧正文甩到文末——
    **每跑一轮文末就多堆一份表头**。所以改用结构定位。
    """
    text = body or ""
    match = _SECTION_HEAD_RE.search(text)
    if match is None:
        return text, ""
    return text[: match.start()], text[match.start() :]


# ---------------------------------------------------------------- 渲染


def render_session(path: Path, *, result: dict[str, Any] | None = None) -> str:
    events = list(read_events(path))
    if not events:
        return "_（session 为空）_"

    start = next((e for e in events if e.get("t") == "run_start"), {})
    end = next((e for e in reversed(events) if e.get("t") == "run_end"), {})
    at = str(end.get("at") or start.get("at") or "")
    kind = str(start.get("kind") or "")
    verdict = str(end.get("verdict") or "—")
    summary = str(end.get("summary") or "")

    lines: list[str] = [f"## {at} · {kind} · `{verdict}`", ""]
    if summary:
        lines += [f"**摘要**：{summary}", ""]

    report = next((e for e in events if e.get("t") == "user"), {})
    counts = _counts_from_report(report.get("content", ""))
    if counts:
        lines += [f"**变更报告**：{counts}", ""]

    usage = end.get("usage") or {}
    lines += [
        "| 指标 | 值 |",
        "|---|---|",
        f"| 步数 | {end.get('steps', 0)} |",
        f"| 工具调用 | {end.get('tool_calls', 0)} |",
        f"| token（入/出） | {usage.get('in', 0)} / {usage.get('out', 0)} |",
        f"| 停止原因 | {end.get('stop_reason', '')} |",
        "",
    ]
    if end.get("error"):
        lines += [f"> ⚠️ **本轮出错**：{end['error']}", ""]

    lines += ["<details><summary>完整时间线（点击展开）</summary>", ""]
    lines += _render_timeline(events)
    lines += ["</details>", ""]

    return "\n".join(lines)


def _render_timeline(events: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    step_seen: dict[int, int] = {}
    for event in events:
        kind = event.get("t")
        if kind == "assistant":
            step = int(event.get("step") or 0)
            step_seen[step] = step_seen.get(step, 0) + 1
            out.append(f"**第 {step} 步 · 模型输出**")
            if event.get("reasoning"):
                out += ["", "🧠 **思考**", "", _quote(event["reasoning"]), ""]
            if event.get("content"):
                out += ["💬 **正文**", "", _quote(event["content"]), ""]
            calls = event.get("tool_calls") or []
            if calls:
                names = "、".join(f"`{c.get('name')}`" for c in calls)
                out += [f"→ 调用工具：{names}", ""]
        elif kind == "tool":
            name = event.get("name")
            ok = event.get("ok")
            mark = "✅" if ok else "❌"
            out += [f"{mark} **工具 `{name}`**（{event.get('ms', 0)} ms）", ""]
            out += [
                "```json",
                _block(json.dumps(event.get("args") or {}, ensure_ascii=False)),
                "```",
                "",
            ]
            body = event.get("result")
            out += ["```json", _block(json.dumps(body, ensure_ascii=False)), "```", ""]
        elif kind == "error":
            out += [f"> ⚠️ {event.get('message')}", ""]
    return out


def _quote(text: str) -> str:
    return "\n".join("> " + line if line.strip() else ">" for line in _block(text).splitlines())


def _block(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_BLOCK_CHARS:
        return text
    return text[:MAX_BLOCK_CHARS] + f"\n…（已截断，全文 {len(text)} 字）"


def _counts_from_report(report_text: str) -> str:
    try:
        payload = json.loads(report_text)
    except (ValueError, TypeError):
        return ""
    counts = payload.get("counts") or {}
    toc = "、目录结构变化" if payload.get("toc_changed") else ""
    return (
        f"新增 {counts.get('added', 0)} / 修改 {counts.get('updated', 0)} / "
        f"删除 {counts.get('removed', 0)}{toc}"
    )


# ---------------------------------------------------------------- 写回语雀


def resolve_journal_doc(client: YuqueClient, title: str) -> dict[str, Any] | None:
    for meta in client.docs():
        if meta.title == title:
            return {"doc_id": meta.doc_id, "slug": meta.slug, "title": meta.title}
    return None


def append_to_journal(
    client: YuqueClient,
    settings: Settings,
    section_markdown: str,
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """把一节内容放到《工作日志》的**最上面**（表头之下、已有各节之前）。"""
    doc_title = title or settings.journal_title
    if settings.dry_run:
        return {"dry_run": True, "doc_title": doc_title, "section_chars": len(section_markdown)}

    existing = resolve_journal_doc(client, doc_title)
    if existing is None:
        # 挂进目录：社员看得到它才能确认自己的文档到底有没有被处理。
        # （归档提示词里已明确写明：《指导文档》与《工作日志》是系统性文档，不要动。）
        created = client.create_doc(title=doc_title, body=f"{HEADER}\n\n{section_markdown}\n")
        doc_id = int((created or {}).get("id") or 0)
        if doc_id:
            client.toc_add(doc_ids=[doc_id])
            client.wait_toc_settled()
        return {"created": True, "doc_id": doc_id, "doc_title": doc_title}

    detail = client.doc(existing["doc_id"])
    head, tail = split_journal(detail.body or "")
    if not head.strip():
        # 文档被清空过（或者压根没有表头）→ 补上，否则日志看起来像天书
        head = HEADER
    new_body = f"{head.rstrip()}\n\n{section_markdown}\n"
    if tail.strip():
        new_body += f"\n{tail.lstrip()}"
    client.update_doc(existing["slug"], body=new_body)
    return {
        "created": False,
        "doc_id": existing["doc_id"],
        "doc_title": doc_title,
        "url": f"{settings.host.rstrip('/')}/{settings.repo}/{existing['slug']}",
    }


def journal_or_warn(
    client: YuqueClient,
    settings: Settings,
    session_path: Path,
    *,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        section = render_session(session_path, result=result)
        return {"ok": True, **append_to_journal(client, settings, section)}
    except (YuqueError, OSError) as exc:
        # 留痕失败不能影响主流程，但必须让人看见
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
