"""对外契约：申请 JSON 与通知事件。

**这两个文件格式是给另外两位同学的接口**，所以这一层刻意写得「死板」：

* 字段名、``schema_version``、``application_id`` / ``notice_id`` / ``seq`` 都由**程序**生成，
  不由 LLM 自由发挥——LLM 只填**内容**，格式由我们保证；
* **``activity`` 对象就是 `crb` 的 ``Activity`` 原样**（字段名、语义完全对齐），
  因为下游是**纯程序**（教室借用插件 / `crb plan --file`），它不会替你猜「仙林」是 `"3"`。
  详见 `docs/handoff.md`；
* 申请**幂等**：``application_id`` 由 ``活动日期 + doc_id`` 推出，同一篇文档重复受理只会覆盖同一个文件；
* 通知**至少一次**：``seq`` 单调递增，文件名带 ``notice_id``，投递方挪走即视为已投递。

```jsonc
{
  "schema_version": "2.0",
  "application_id": "2026-09-23-285808038",
  "source":   { ... 哪篇语雀文档、谁写的、内容指纹 ... },
  "activity": { title, date, period, people, campus, building, room_type, preferred_room },
  "raw":      { ... 社员原话，供人工复核 ... },
  "derived":  { ... 程序推导过程，如 教学楼名→JXLDM、时间→节次 ... },
  "agent":    { ... 哪个 run、置信度、agent 自己的备注 ... }
}
```
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from . import clock
from .config import Settings

APPLICATION_SCHEMA_VERSION = "2.0"
NOTICE_SCHEMA_VERSION = "1.0"

#: ``activity`` 里必须有值的字段（其余可空即「随机」）。
APPLICATION_REQUIRED = ("title", "date", "period", "campus")

#: 下游（教室借用插件 / crb）真正会读的字段。多出来的键会被忽略。
CRB_ACTIVITY_FIELDS = (
    "title",
    "date",
    "period",
    "people",
    "campus",
    "building",
    "room_type",
    "preferred_room",
)

NOTICE_KINDS = (
    "accepted",
    "rejected",
    "unrecognized",
    "tampered",
    "deleted",
    "info",
)


class ContractError(ValueError):
    """LLM 给出的产出不符合契约。消息会被原样返回给 LLM 让它自我纠正。"""


def now_iso() -> str:
    return clock.stamp()


# ---------------------------------------------------------------- 申请


def make_application_id(activity_date: str, doc_id: int | str) -> str:
    """``2026-09-16`` + ``1234567`` → ``2026-09-16-1234567``。

    稳定、可排序、可读；同一篇文档重复受理不会产生新 id。
    """
    date = re.sub(r"[^0-9-]", "", str(activity_date)) or "unknown-date"
    return f"{date}-{doc_id}"


def write_application(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    """落盘一份申请。返回给 LLM 的摘要（含路径），便于它确认。

    ``payload`` 必须是 :mod:`.tools` 里 :func:`_emit_application` 组装好的结构：
    ``activity`` 已经是对齐 crb 的形状，这里只做**最后一道契约校验**。
    """
    activity = payload.get("activity")
    source = payload.get("source")
    if not isinstance(activity, dict) or not isinstance(source, dict):
        raise ContractError("payload 必须同时包含 source 与 activity 两个对象")
    if not source.get("doc_id"):
        raise ContractError("source.doc_id 必填")
    missing = [key for key in APPLICATION_REQUIRED if not activity.get(key)]
    if missing:
        raise ContractError(f"activity 缺少必填字段：{missing}")
    if not activity.get("date"):
        raise ContractError("activity.date 必填（YYYY-MM-DD）")

    application_id = make_application_id(activity["date"], source["doc_id"])
    record = {
        "schema_version": APPLICATION_SCHEMA_VERSION,
        "application_id": application_id,
        "created_at": now_iso(),
        "source": source,
        "activity": {key: activity.get(key) for key in CRB_ACTIVITY_FIELDS},
        "raw": payload.get("raw") or {},
        "derived": payload.get("derived") or {},
        "agent": payload.get("agent") or {},
        "normalizations": payload.get("normalizations") or [],
        "warnings": payload.get("warnings") or [],
    }

    path = settings.applications_dir / f"{application_id}.json"
    replaced = path.exists()
    if not settings.dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2))
        rebuild_application_index(settings)

    return {
        "application_id": application_id,
        "path": str(path),
        "replaced_existing": replaced,
        "campus": record["activity"].get("campus"),
        "building": record["activity"].get("building") or "(空=随机)",
        "period": record["activity"].get("period"),
        "dry_run": settings.dry_run,
    }


def build_plan_json(
    settings: Settings,
    *,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 ``outbox/applications/`` 里的申请汇总成下游可直接吃的 ``plan.json``。

    下游用法（以 crb 为例）::

        yqa export-plan -o plan.json
        crb plan --file plan.json            # 先看方案
        crb plan --file plan.json --save     # 再存草稿
    """
    activities: list[dict[str, Any]] = []
    for path in sorted(settings.applications_dir.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        activity = data.get("activity")
        if not isinstance(activity, dict):
            continue
        entry = {key: activity.get(key) for key in CRB_ACTIVITY_FIELDS}
        # 把溯源信息一并带上，方便下游出问题时回查（crb 会忽略这些键）
        entry["_application_id"] = data.get("application_id")
        entry["_doc_id"] = (data.get("source") or {}).get("doc_id")
        activities.append(entry)
    activities.sort(key=lambda a: (str(a.get("date") or ""), str(a.get("_doc_id") or "")))
    return {"defaults": defaults or {}, "activities": activities}


def rebuild_application_index(settings: Settings) -> None:
    """索引由目录扫描**重建**，避免「副本与正文漂移」。"""
    rows = []
    for path in sorted(settings.applications_dir.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        activity = data.get("activity") or {}
        rows.append(
            {
                "application_id": data.get("application_id", path.stem),
                "date": activity.get("date", ""),
                "period": activity.get("period", ""),
                "campus": activity.get("campus", ""),
                "title": activity.get("title", ""),
                "doc_id": (data.get("source") or {}).get("doc_id"),
                "file": path.name,
            }
        )
    rows.sort(key=lambda r: (r["date"], str(r["doc_id"])))
    _atomic_write(
        settings.applications_dir / "index.json",
        json.dumps({"count": len(rows), "applications": rows}, ensure_ascii=False, indent=2),
    )


# ---------------------------------------------------------------- 通知


def write_notice(settings: Settings, *, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """落盘一条待投递的通知事件。"""
    if kind not in NOTICE_KINDS:
        raise ContractError(f"kind 必须是 {list(NOTICE_KINDS)} 之一，收到 {kind!r}")
    summary = str(payload.get("summary") or "").strip()
    message = str(payload.get("message") or "").strip()
    if not summary:
        raise ContractError("summary 必填（一句话，给社员看的标题）")
    if not message:
        raise ContractError("message 必填（给社员看的完整中文正文，QQ 里直接发这条）")

    seq = _next_seq(settings)
    notice_id = _short_hash(f"{kind}\x00{summary}\x00{message}\x00{seq}")
    record = {
        "schema_version": NOTICE_SCHEMA_VERSION,
        "seq": seq,
        "notice_id": notice_id,
        "created_at": now_iso(),
        "kind": kind,
        "repo": settings.repo,
        "doc": payload.get("doc") or {},
        "member": payload.get("member") or {},
        "summary": summary,
        "message": message,
        "reasons": payload.get("reasons") or [],
        "warnings": payload.get("warnings") or [],
        "extra": payload.get("extra") or {},
    }

    path = settings.notify_dir / "pending" / f"{seq:06d}-{kind}-{notice_id}.json"
    if not settings.dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2))
        _append_outbox(settings, record)

    return {
        "seq": seq,
        "notice_id": notice_id,
        "kind": kind,
        "path": str(path),
        "dry_run": settings.dry_run,
    }


def _next_seq(settings: Settings) -> int:
    counter = settings.notify_dir / ".seq"
    current = 0
    try:
        current = int(counter.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        current = 0
    # 兜底：目录里已经有更大的 seq（比如 counter 被删了）就别倒退
    for folder in ("pending", "done"):
        for path in (settings.notify_dir / folder).glob("*.json"):
            head = path.name.split("-", 1)[0]
            if head.isdigit():
                current = max(current, int(head))
    nxt = current + 1
    if not settings.dry_run:
        counter.parent.mkdir(parents=True, exist_ok=True)
        counter.write_text(str(nxt), encoding="utf-8")
    return nxt


def _append_outbox(settings: Settings, record: dict[str, Any]) -> None:
    """只追加的审计流水；投递方**不要**读它，否则会重复投递。"""
    path = settings.notify_dir / "outbox.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 工具


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _short_hash(text: str, length: int = 8) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]
