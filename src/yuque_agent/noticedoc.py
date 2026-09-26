"""《Agent 通知》文档：本周期内 agent 发出去的处理通知。

它是**程序维护**的（不是 LLM 写的）。每个周期一份，周期更替时清空：

* 内容 = 本周期内 ``outbox/notify/{pending,done,unrouted,failed}/`` 里的通知；
* 给 cac（指导老师）的「申请清单已更新」（``plan_updated``）**不在**这里——那是另一件事；
* 最新的在最上面。

**为什么是「重建」而不是「追加」**：内容成了「本周期通知」的纯函数——
周期一翻，重建出来的自然是空的（清空不需要额外动作），
手动归档、重复跑、漏跑一轮都不会让内容漂。

`runner` 在两处调 :func:`refresh`：每轮跑完（把这一轮发出的通知写进去）、
周期翻转时（让上一周期的内容立刻消失）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from . import outputs
from .config import Settings
from .week import cycle_of
from .yuque import YuqueClient, YuqueError

#: 通知文件可能待着的四个目录（投递方搬来搬去，不改变「通知属于哪个周期」）。
NOTICE_DIRS = ("pending", "done", "unrouted", "failed")

#: ``kind`` → 给人看的动作名（只是展示，不是判断）。
KIND_LABELS = {
    "accepted": "已受理",
    "rejected": "已退回",
    "unrecognized": "看不出是申请",
    "tampered": "改动无效",
    "deleted": "文档被删",
    "info": "提醒",
}


def load_notices(settings: Settings) -> list[dict[str, Any]]:
    """读出所有通知（四个目录，按 ``notice_id`` 去重），**最新在前**。"""
    seen: dict[str, dict[str, Any]] = {}
    for folder in NOTICE_DIRS:
        for path in sorted((settings.notify_dir / folder).glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                seen.setdefault(str(data.get("notice_id") or path.name), data)
    return sorted(seen.values(), key=lambda n: int(n.get("seq") or 0), reverse=True)


def cycle_of_notice(settings: Settings, notice: dict[str, Any]) -> str:
    """一条通知属于哪个周期（按它的 ``created_at`` 算，与归档用同一套算术）。"""
    try:
        day = datetime.fromisoformat(str(notice.get("created_at") or "")).date()
    except ValueError:
        return ""
    return cycle_of(day, start_weekday=settings.archive_weekday).title


def notices_for_cycle(settings: Settings, cycle: str) -> list[dict[str, Any]]:
    """本周期内、且该进《Agent 通知》的那些（最新在前）。"""
    return [
        n
        for n in load_notices(settings)
        if str(n.get("kind")) != "plan_updated"  # 给 cac 的清单提醒不进这里
        and cycle_of_notice(settings, n) == cycle
    ]


def render(settings: Settings, cycle: str) -> str:
    """把本周期该展示的通知渲染成 markdown（**说人话**：没有工具调用、没有思考过程）。

    头部那两行是**需求方审过稿的**（2026-09-26，从四行压到一行）——
    改它等于改知识库里那篇文档的正文，下一次刷新就会覆盖到 KB 上。
    """
    items = notices_for_cycle(settings, cycle)
    lines = [
        "# Agent 通知",
        "> 本周期内 agent 发出的处理通知，**最新的在最上面**。",
        ">",
        "",
    ]
    if not items:
        lines += ["（本周期还没有通知。）", ""]
        return "\n".join(lines)
    for notice in items:
        lines += _render_item(notice)
    return "\n".join(lines)


def _render_item(notice: dict[str, Any]) -> list[str]:
    kind = str(notice.get("kind") or "")
    when = str(notice.get("created_at") or "")[:16].replace("T", " ")
    doc = notice.get("doc") or {}
    title = str(doc.get("title") or "")
    member = str((notice.get("member") or {}).get("name") or "")

    lines = [f"## {when} · {KIND_LABELS.get(kind, kind or '通知')}", ""]
    if title:
        lines += [f"**《{title}》**" + (f"（{member}）" if member else ""), ""]
    if message := str(notice.get("message") or "").strip():
        lines += [message, ""]
    if reasons := [str(r) for r in (notice.get("reasons") or []) if str(r).strip()]:
        lines += ["原因：" + "；".join(reasons), ""]
    lines += ["---", ""]
    return lines


def refresh(settings: Settings, client: YuqueClient, *, cycle: str = "") -> dict[str, Any]:
    """把《Agent 通知》重建成 ``cycle``（默认=当下）那一份。**幂等**。

    文档不存在就建一个（挂进根目录；位置由归档会话按目标顺序保持）；
    内容没变就一个字都不写。任何失败都只记进返回值，不打断这一轮。
    """
    cycle = cycle or outputs.current_cycle(settings)
    count = len(notices_for_cycle(settings, cycle))
    body = render(settings, cycle)
    if settings.dry_run:
        return {"ok": True, "dry_run": True, "cycle": cycle, "count": count}

    try:
        existing = next((m for m in client.docs() if m.title == settings.notice_title), None)
        if existing is None:
            created = client.create_doc(title=settings.notice_title, body=body)
            doc_id = int((created or {}).get("id") or 0)
            if doc_id:
                client.toc_add(doc_ids=[doc_id])
                client.wait_toc_settled()
            return {"ok": True, "created": True, "doc_id": doc_id, "cycle": cycle, "count": count}
        if (client.doc(existing.doc_id).body or "").strip() == body.strip():
            return {
                "ok": True,
                "created": False,
                "unchanged": True,
                "doc_id": existing.doc_id,
                "cycle": cycle,
                "count": count,
            }
        client.update_doc(existing.slug, body=body)
        return {
            "ok": True,
            "created": False,
            "unchanged": False,
            "doc_id": existing.doc_id,
            "cycle": cycle,
            "count": count,
        }
    except (YuqueError, OSError) as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "cycle": cycle,
            "count": count,
        }
