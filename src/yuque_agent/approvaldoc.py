"""《审批结果》文档：教室借用申请**审批结束**的结果（通过 / 退回）。

它是**程序维护**的（不是 LLM 写的），和《Agent 通知》同一个模子：
**内容是文件的纯函数**，每轮重建，所以周期翻滚、手动重跑、漏跑一轮都不会让内容漂。

数据来自下游 ``crb-notify``：它接收浏览器插件读到的申请列表，把「结束了」的挑出来
写进工作区的 ``outbox/approval/``：

* ``notifications.json`` —— 对外文档（``nova.classroom-borrow-notification.v1``），
  这个模块读它渲染；
* ``unmatched.json`` —— 认不出来的（日期/节次/标题对不上语雀那边），**不进正文**，
  但会在末尾提一句「有几条需要人看」。

为什么不由本仓库直接去查学校系统：本项目**不持有学校登录态**（见
``docs/handoff.md`` §1.1），那是刻意的边界。

> 正文必须与「语雀读回来的形式」逐字一致，否则每轮刷新都会判「内容变了」
> 而白写一次（``noticedoc`` 2026-09-26 实测踩到：多一个空行就刷屏）。
> 所以这里只生成一种形状，且不在标题与正文之间留空行。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .yuque import YuqueClient, YuqueError

#: 每条结果最多渲染多少条（文档是给人看的，不是审计日志；超了只列最近的）。
MAX_ITEMS = 200


def approval_dir(settings: Settings) -> Path:
    """下游 ``crb-notify`` 的产出目录。"""
    return settings.plan_file.parent / "approval"


def load_notifications(settings: Settings) -> dict[str, Any]:
    """读 ``notifications.json``。读不到就当成空（还没结束的申请）。"""
    path = approval_dir(settings) / "notifications.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_unmatched_count(settings: Settings) -> int:
    """认不出来的条数（``unmatched.json``）。"""
    path = approval_dir(settings) / "unmatched.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    items = data.get("unmatched") if isinstance(data, dict) else None
    return len(items) if isinstance(items, list) else 0


def _stamp(value: Any) -> str:
    """``2026-09-27T10:25:30+08:00`` → ``2026-09-27 10:25``（保持原时区，别自己换算）。"""
    text = str(value or "")
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text[:16].replace("T", " ")


def render(settings: Settings) -> str:
    """把审批结果渲染成 markdown（**说人话**：没有工具调用、没有内部批号）。"""
    document = load_notifications(settings)
    items = [i for i in (document.get("notifications") or []) if isinstance(i, dict)]
    # 最新的在最上面（和《Agent 通知》一致；这份文档里「新」指检测到结果的时刻）
    items = sorted(items, key=lambda i: str(i.get("detectedAt") or ""), reverse=True)[:MAX_ITEMS]

    # 头部**不带大标题**：语雀文档自己就有一个标题（《审批结果》）。
    lines = [
        "> 教室借用申请的审批结果，**最新的在最上面**。",
        "> 由程序自动维护（浏览器插件读到申请列表，`crb-notify` 判定），**请勿手工编辑**。",
        "",
    ]
    if not items:
        lines += ["（还没有审批结束的申请。）", ""]
    for item in items:
        lines += _render_item(item)

    unmatched = load_unmatched_count(settings)
    if unmatched:
        lines += [
            f"> ⚠️ 还有 **{unmatched}** 条结果认不出对应的活动（日期/节次/标题对不上），",
            "> 需要人看一眼；它们**不在**上面的清单里，也不会被通知出去。",
            "",
        ]
    return "\n".join(lines)


def _render_item(item: dict[str, Any]) -> list[str]:
    activity = item.get("activity") or {}
    result = item.get("result") or {}
    approved = str(item.get("type")) == "approved"
    head = "已通过" if approved else "已退回"
    title = str(activity.get("title") or "（无标题）")
    when = _stamp(item.get("detectedAt"))

    # 标题行与下一行之间**不留空行**——语雀读回来会把空行规范化掉，
    # 逐字对齐才不会每轮都白写一次。
    lines = [f"## {title} · {head}"]
    slot_start = str(activity.get("slotStart") or "")
    slot_end = str(activity.get("slotEnd") or "")
    slot = slot_start if slot_start == slot_end or not slot_end else f"{slot_start} - {slot_end}"
    lines.append(
        "　".join(
            part
            for part in (
                str(activity.get("date") or ""),
                slot,
                str(activity.get("campus") or ""),
                f"借用人：{activity['organizer']}" if activity.get("organizer") else "",
            )
            if part
        )
    )
    if approved:
        rooms = "、".join(str(r) for r in (result.get("actualRooms") or [])) or "（待定）"
        lines.append(f"教室：**{rooms}**")
    else:
        lines.append(f"原因：{result.get('feedback') or '（学校未给原因）'}")
    if when:
        lines.append(f"检测于 {when}")
    lines += ["", "---", ""]
    return lines


def refresh(settings: Settings, client: YuqueClient) -> dict[str, Any]:
    """把《审批结果》重建一次。**幂等**：内容没变就一个字都不写。

    文档不存在就建一个（挂进根目录；位置由归档会话按 ``root_target_order`` 保持）。
    任何失败都只记进返回值，不打断这一轮 —— 和《Agent 通知》一样。
    """
    body = render(settings)
    count = len(load_notifications(settings).get("notifications") or [])
    if settings.dry_run:
        return {"ok": True, "dry_run": True, "count": count}

    try:
        existing = next((m for m in client.docs() if m.title == settings.approval_title), None)
        if existing is None:
            created = client.create_doc(title=settings.approval_title, body=body)
            doc_id = int((created or {}).get("id") or 0)
            if doc_id:
                client.toc_add(doc_ids=[doc_id])
                client.wait_toc_settled()
            return {"ok": True, "created": True, "doc_id": doc_id, "count": count}
        if (client.doc(existing.doc_id).body or "").strip() == body.strip():
            return {
                "ok": True,
                "created": False,
                "unchanged": True,
                "doc_id": existing.doc_id,
                "count": count,
            }
        client.update_doc(existing.slug, body=body)
        return {
            "ok": True,
            "created": False,
            "unchanged": False,
            "doc_id": existing.doc_id,
            "count": count,
        }
    except (YuqueError, OSError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "count": count}
