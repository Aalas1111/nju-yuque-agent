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
import shutil
from pathlib import Path
from typing import Any

from . import clock
from .config import Settings
from .week import cycle_of

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
    "plan_updated",
)
"""所有合法的通知类型。其中有几个**只能由程序发**，见下。"""

PROGRAM_NOTICE_KINDS = ("plan_updated",)
"""**程序专属**的通知类型：LLM 不许发。

为什么单列一类：`plan_updated`（「申请清单已更新，cac 请尽快下载提交」）
要的是给**管理员**的正向提醒，而 LLM 对「清单变了」这件事没有可靠视角——
它只知道这一轮改了哪几篇文档，不知道清单整体长什么样、有没有过期。
由 :func:`write_application` 在写盘后顺手发，才不会漏。

**能力闸门**：``tools.emit_notice`` 校验的是 ``LLM_NOTICE_KINDS``，
所以 LLM 根本发不出这一类（不是靠提示词叮嘱）。
"""

LLM_NOTICE_KINDS = tuple(k for k in NOTICE_KINDS if k not in PROGRAM_NOTICE_KINDS)
"""LLM 能发的类型（= 提示词里必须出现的那几个）。"""


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
        # 这份申请属于哪个申请周期。**程序打上去的**，不给 LLM 填——
        # 周期是纯算术（见 week.cycle_of），而它决定了这份申请什么时候被归档。
        "cycle": current_cycle(settings),
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
        # 下游（cac）拿不到推送，只能靠这个文件是新的。所以**每次写申请都重发**。
        publish_plan(settings)

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
    """把**当前周期**的申请汇总成下游可直接吃的 ``plan.json``。

    注意作用域：这里扫的是 ``outbox/applications/``，而**那个目录就是当前周期**
    （周期翻转时 :func:`rotate_outbox` 会把整批搬进 ``archive/<周期>/``）。
    所以这里**不需要**再做日期过滤——目录边界就是周期边界，
    这正是「程序负责可测的事实」：让路径承担语义，而不是让下游猜。

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
    return {
        "cycle": current_cycle(settings),
        "generated_at": now_iso(),
        "defaults": defaults if defaults is not None else read_plan_defaults(settings),
        "activities": activities,
    }


def current_cycle(settings: Settings) -> str:
    """今天是哪个申请周期（``0919-0925``）。一周之内稳定不变。"""
    return cycle_of(clock.today(), start_weekday=settings.archive_weekday).title


# ------------------------------------------------------- 交付件 plan.json


def read_plan_defaults(settings: Settings) -> dict[str, Any]:
    """借用人信息（``JYRXM`` / ``JYRDH`` …）。**落盘保存**，否则每次自动重发都会丢。"""
    try:
        data = json.loads(settings.plan_defaults_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_plan_defaults(settings: Settings, defaults: dict[str, Any]) -> None:
    if not defaults or settings.dry_run:
        return
    settings.plan_defaults_file.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(settings.plan_defaults_file, json.dumps(defaults, ensure_ascii=False, indent=2))


def publish_plan(settings: Settings, *, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """把当前周期的计划写到 ``outbox/plan.json``（cac 就取这个文件）。

    为什么要在**每次写申请**后调：下游拿不到推送，它只能看到「文件是不是新的」。
    自动重发比让服务去定时重发更准——没变动时时间戳不会乱跳。
    """
    plan = build_plan_json(settings, defaults=defaults)
    if not settings.dry_run:
        settings.plan_file.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(settings.plan_file, json.dumps(plan, ensure_ascii=False, indent=2))
    return plan


def rotate_outbox(settings: Settings, *, cycle: str) -> dict[str, Any]:
    """把活跃期的产物整批搬进 ``archive/<cycle>/``。**程序做，不靠 LLM。**

    为什么不交给 LLM 的归档会话：归档会话会失败（网络、模型、token 上限），
    而**产物边界不能跟着它一起失败**——一旦没搬，上个周期的申请就会留在
    ``plan.json`` 里被 cac 当成这周的提交上去。周期是纯算术，所以这件事
    归程序，而且不花一个 token。

    搬法和活跃期**完全同形**，所以「那周到底交付了什么」以后能原样翻出来::

        archive/<cycle>/plan.json          当周那个版本（冻结）
        archive/<cycle>/applications/*.json
        archive/<cycle>/applications/index.json

    幂等：同一周期重复调不会丢文件（同名文件个别搬）。
    """
    dest = settings.cycle_archive_dir(cycle)
    moved: list[str] = []

    def _move(src: Path, dst_dir: Path) -> None:
        dst_dir.mkdir(parents=True, exist_ok=True)
        target = dst_dir / src.name
        if target.exists():
            # 同周期已经搬过一次：不覆盖，按修改时间排下去，保证两个都留底
            stamp = clock.compact_stamp()
            target = dst_dir / f"{src.stem}.{stamp}{src.suffix}"
        shutil.move(str(src), str(target))
        moved.append(src.name)

    if settings.applications_dir.is_dir():
        for path in sorted(settings.applications_dir.iterdir()):
            if path.is_file():
                _move(path, dest / "applications")
        # 目录本身留着（下游与 reset 都指望它在）
    if settings.plan_file.is_file():
        _move(settings.plan_file, dest)

    if moved:
        settings.ensure_dirs()
        rebuild_application_index(settings)
    return {"cycle": cycle, "dest": str(dest), "moved": moved}


def plan_fingerprint(plan: dict[str, Any]) -> str:
    """计划内容的指纹（只看 ``activities``，不看时间戳）。

    用来判断「清单是不是真变了」——否则每轮都会因为 ``generated_at`` 变了
    而给管理员重发一条「请下载」。
    """
    blob = json.dumps(plan.get("activities") or [], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_plan_updated_notice(plan: dict[str, Any], *, member: str = "") -> dict[str, Any]:
    """拼一条给**管理员**的「清单已更新」通知（cac 看了就知道要下载）。

    注意这是**提醒**，不是「已办结」——我们无从知道 cac 到底下没下、交没交，
    所以文案只能请他去下，不能声称已处理（那样会把「至少一次」变成「至多一次」）。
    """
    activities = plan.get("activities") or []
    dates = sorted({str(a.get("date") or "") for a in activities if a.get("date")})
    span = f"{dates[0]} ~ {dates[-1]}" if dates else "（无）"
    cycle = str(plan.get("cycle") or "")
    return {
        "summary": f"申请清单已更新（{cycle}）",
        "message": (
            f"【申请清单已更新】周期 {cycle}，共 {len(activities)} 条，活动日期 {span}。\n"
            "请尽快下载 outbox/plan.json 并提交，逾期不补。\n"
            "（下载方式：网页用密钥取，或从服务器 scp。）"
        ),
        "member": {"name": member},
        "extra": {
            "cycle": cycle,
            "count": len(activities),
            "dates": dates,
            "plan_fingerprint": plan_fingerprint(plan),
        },
    }


def detect_active_cycle(settings: Settings) -> str:
    """从活跃申请自带的 ``cycle`` 字段推断「这批属于哪个周期」。

    只用于**升级前的存量**——那时申请里还没写 ``cycle``、state 里也没记录，
    而如果直接当成「当前周期」，上个周期的申请就永远不会被归档。
    取出现次数最多的那个；一个都没有（空目录 / 老格式）就返回 ``""``。
    """
    counts: dict[str, int] = {}
    for path in sorted(settings.applications_dir.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cycle = str(data.get("cycle") or "").strip()
        if cycle:
            counts[cycle] = counts.get(cycle, 0) + 1
    if not counts:
        return ""
    return max(counts, key=lambda c: (counts[c], c))


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
