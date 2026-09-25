"""工具注册表——**本项目的安全闸门就在这里**。

核心设计（也是本项目要研究的那个问题）：

> 「不许 agent 改知识库结构」不是靠提示词求它自律，而是靠**本轮根本没注册那个工具**。

于是 :data:`COMMON_TOOLS` 里**一个语雀写工具都没有**；只有
:data:`ARCHIVE_TOOLS`（每周六归档会话）才把 ``toc_*`` / ``doc_delete`` 挂上去。
日常轮询时 agent 手里没有刀，「误删文档」在物理上不可能发生。

另外两条：

* 所有文件写操作走 :func:`config.safe_join`，越界直接报错返回给 LLM 自己纠正；
* 工具**返回错误而不是抛异常**——让 LLM 看得见失败并自己想办法，这本身就是研究素材。

**本层只做「能力边界」（粗粒度：有 / 没有这个工具），不做对 LLM 输出的逐字审查**——
文案 / 语义类规矩只写进提示词（理由见 ``docs/principles.md`` §4）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from . import outputs, school
from .config import Settings, safe_join
from .outputs import ContractError
from .session import Stopwatch
from .snapshot import DocSnapshot
from .yuque import YuqueClient, YuqueError, dir_map_from_payload

MAX_DOC_CHARS = 8000
MAX_LISTING = 200

DEFAULT_PEOPLE = 30
"""人数未填时的缺省值（与《指导文档》一致）。"""


class ToolError(RuntimeError):
    """工具执行失败。消息会原样回给 LLM。"""


# ---------------------------------------------------------------- 运行上下文


@dataclass
class RunContext:
    settings: Settings
    client: YuqueClient
    run_id: str
    kind: str
    """``polling`` | ``archive``"""
    run_dir: Path
    toc: list[dict[str, Any]] = field(default_factory=list)
    docs: dict[int, DocSnapshot] = field(default_factory=dict)
    today: date | None = None
    """本轮「今天」（取自变更报告的 ``at``，便于测试注入）。"""

    finished: bool = False
    verdict: str = ""
    summary: str = ""
    emitted: list[dict[str, Any]] = field(default_factory=list)
    kb_writes: list[dict[str, Any]] = field(default_factory=list)

    _all_docs: list[Any] | None = field(default=None, repr=False)

    # -- 懒加载 -----------------------------------------------------------
    def all_docs(self) -> list[Any]:
        if self._all_docs is None:
            self._all_docs = self.client.docs()
        return self._all_docs

    def dir_of(self, doc_id: int) -> str:
        doc = self.docs.get(doc_id)
        if doc is not None:
            return doc.dir
        for node in self.toc:
            if node.get("doc_id") == doc_id:
                return str(node.get("path") or "")
        return ""

    def author_of(self, doc_id: int) -> str:
        doc = self.docs.get(doc_id)
        return doc.author if doc is not None else ""

    def sha_of(self, doc_id: int) -> str:
        doc = self.docs.get(doc_id)
        return doc.content_sha256 if doc is not None else ""

    def note_kb_write(self, op: str, detail: dict[str, Any]) -> None:
        """记下对语雀的每一次结构改动（dry_run 时是「本来要做」）。"""
        self.kb_writes.append({"op": op, **detail})


# ---------------------------------------------------------------- 工具定义


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[RunContext, dict[str, Any]], Any]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _params(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}
STRLIST = {"type": "array", "items": {"type": "string"}}


# ---------------------------------------------------------------- 读工具


def _kb_tree(ctx: RunContext, args: dict[str, Any]) -> Any:
    """知识库目录树（含每个目录下的文档数）。"""
    counts: dict[str, int] = {}
    for path in _doc_dirs(ctx).values():
        counts[path] = counts.get(path, 0) + 1
    return {
        "nodes": [
            {
                "uuid": n.get("uuid"),
                "type": n.get("type"),
                "title": n.get("title"),
                "path": n.get("path"),
                "depth": n.get("depth"),
                "docs_here": counts.get(str(n.get("path") or ""), 0),
            }
            for n in ctx.toc
            if n.get("type") != "DOC"
        ],
        "total_docs_known": len(ctx.docs),
    }


def _dir_list(ctx: RunContext, args: dict[str, Any]) -> Any:
    """列出某个目录下的全部文档（不只是本轮变更过的）。"""
    wanted = str(args.get("dir") or "").strip()
    if not wanted:
        raise ToolError("dir 必填，例如 '0919-0925' 或 '归档区/0912-0918'；根目录用 '.'")
    root_wanted = wanted in (".", "/", "根目录", "(根目录)")
    dir_by_doc = _doc_dirs(ctx)
    rows = []
    for meta in ctx.all_docs():
        path = dir_by_doc.get(meta.doc_id)
        if path is None:
            continue
        # 注意：不能把 ``path == ""``（根目录下的文档）当成「匹配任意目录」。
        # 实测踩到过：之前写成 ``path == wanted or path.endswith("/"+wanted) or path == ""``，
        # 结果 ``dir_list("归档区/0912-0918")`` 会把根目录的《指导文档》《工作日志》
        # 一并返回——归档会话要是信了这个结果，就可能把系统性文档从根目录搬走。
        if root_wanted:
            matched = path == ""
        else:
            matched = path == wanted or path.endswith("/" + wanted)
        if not matched:
            continue
        rows.append(
            {
                "doc_id": meta.doc_id,
                "title": meta.title,
                "author": meta.author,
                "updated_at": meta.updated_at,
                "dir": path,
            }
        )
    rows.sort(key=lambda r: r["updated_at"], reverse=True)
    return {"dir": wanted, "count": len(rows), "docs": rows[:MAX_LISTING]}


def _doc_dirs(ctx: RunContext) -> dict[int, str]:
    """``doc_id -> 所在目录路径``（根目录是空串）。

    算法只有一份，在 :func:`yuque.dir_map_from_payload`：**看节点的 ``parent_uuid``**，
    不去切 ``path`` 字符串——文档标题里可以带 ``/``（实测踩到过，见
    ``tests/test_dir_list.py`` 的回归测试）。

    「算文档在哪个目录」这件事曾经有三份实现、两份是错的（`docs/test-report.md`），
    所以这里只允许有一条路。
    """
    return dir_map_from_payload(ctx.toc)


def _doc_read(ctx: RunContext, args: dict[str, Any]) -> Any:
    """读一篇文档的正文（markdown）。"""
    ref = args.get("doc")
    if ref in (None, ""):
        raise ToolError("doc 必填（doc_id 或 slug）")
    try:
        detail = ctx.client.doc(int(ref) if str(ref).isdigit() else str(ref))
    except YuqueError as exc:
        raise ToolError(f"读取失败：{exc}") from exc
    body = detail.body or ""
    truncated = len(body) > MAX_DOC_CHARS
    return {
        "doc_id": detail.doc_id,
        "slug": detail.slug,
        "title": detail.title,
        "author": detail.author,
        "updated_at": detail.updated_at,
        "dir": ctx.dir_of(detail.doc_id),
        "body": body[:MAX_DOC_CHARS],
        "truncated": truncated,
        "note": f"正文超过 {MAX_DOC_CHARS} 字已截断" if truncated else "",
    }


# ---------------------------------------------------------------- 工作区工具


def _ws_list(ctx: RunContext, args: dict[str, Any]) -> Any:
    """列工作区里的文件（看清自己之前留下了什么）。"""
    rel = str(args.get("path") or ".")
    try:
        base = safe_join(ctx.settings.root, rel)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if not base.exists():
        raise ToolError(f"不存在：{rel}")
    entries = []
    paths = [base] if base.is_file() else sorted(base.rglob("*"))
    for path in paths[:MAX_LISTING]:
        if path.name.endswith(".tmp"):
            continue
        entries.append(
            {
                "path": str(path.relative_to(ctx.settings.root)).replace("\\", "/"),
                "dir": path.is_dir(),
                "size": 0 if path.is_dir() else path.stat().st_size,
            }
        )
    return {"root": rel, "entries": entries}


def _ws_read(ctx: RunContext, args: dict[str, Any]) -> Any:
    """读工作区里的一个文件（比如上一轮产出的申请 JSON）。"""
    rel = str(args.get("path") or "")
    try:
        path = safe_join(ctx.settings.root, rel)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if not path.is_file():
        raise ToolError(f"不是文件或不存在：{rel}")
    text = path.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > MAX_DOC_CHARS
    return {"path": rel, "content": text[:MAX_DOC_CHARS], "truncated": truncated}


def _ws_write(ctx: RunContext, args: dict[str, Any]) -> Any:
    """在你的私人记事本 ``notes/`` 下写文件（跨轮记忆）。"""
    rel = str(args.get("path") or "")
    content = str(args.get("content") or "")
    if not rel.startswith("notes/"):
        rel = f"notes/{rel.lstrip('/')}"
    try:
        path = safe_join(ctx.settings.notes_dir, rel[len("notes/") :])
    except ValueError as exc:
        raise ToolError(f"只能写在 notes/ 下：{exc}") from exc
    if ctx.settings.dry_run:
        return {"path": rel, "bytes": len(content.encode("utf-8")), "dry_run": True}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"path": rel, "bytes": len(content.encode("utf-8")), "dry_run": False}


# ---------------------------------------------------------------- 产出工具


def _emit_application(ctx: RunContext, args: dict[str, Any]) -> Any:
    """产出一份「要素齐备」的教室借用申请，交给负责提交的同学。

    分工：**LLM 给「社员原话」（校区名/教学楼名/时间），程序做查表归一化**
    （校区名→代码、教学楼名→`JXLDM`、`HH:MM`→节次）。
    产出里的 ``activity`` 就是 `crb` 的 ``Activity`` 原样，下游可直接吃。
    """
    doc_id = _need(args, "doc_id")
    title = str(args.get("activity_name") or args.get("doc_title") or "").strip()
    if not title:
        raise ToolError("activity_name 必填（活动名称）")

    day_text = str(args.get("activity_date") or "").strip()
    try:
        activity_day = date.fromisoformat(day_text)
    except ValueError as exc:
        raise ToolError(f"activity_date 必须是 YYYY-MM-DD，收到 {day_text!r}") from exc

    start = str(args.get("start") or "").strip()
    end = str(args.get("end") or "").strip()
    if school.parse_time(start) is None or school.parse_time(end) is None:
        raise ToolError(f"start / end 必须是 24 小时制 HH:MM，收到 {start!r} / {end!r}")
    span = school.periods_for(start, end)
    if span is None:
        raise ToolError(
            f"{start}-{end} 对不上任何节次（可借时段 08:00–22:20，其中 12:00–14:00 是午休）。"
            "如果你判断这个时间确实没法借，请改用 emit_notice 退回，不要 emit_application。"
        )
    ksjc, jsjc = span

    campus_text = str(args.get("campus") or "").strip()
    campus_code = school.normalize_campus(campus_text)
    if not campus_code:
        raise ToolError(
            f"校区 {campus_text!r} 认不出来。只能是 鼓楼 / 浦口 / 仙林 / 苏州 之一"
            "（NOVA 的活动基本都在仙林和苏州）。不确定的话请用 emit_notice 退回确认。"
        )

    building_text = str(args.get("building") or "").strip()
    building_code, building_note = school.normalize_building(campus_code, building_text)
    room_text = str(args.get("room") or "").strip()

    people = int(args.get("people") or 0)
    people_source = "document" if people else "default"
    if people <= 0:
        people = DEFAULT_PEOPLE

    warnings = [str(w) for w in (args.get("warnings") or [])]
    normalizations = [str(n) for n in (args.get("normalizations") or [])]

    # 日期范围的「提醒」由程序算（校方规则：今天 +2 ~ +9 天），但不阻止受理。
    today = ctx.today
    if today is None:
        warnings.append("无法验证日期范围：本轮缺少当前日期信息（ctx.today 为空）")
    else:
        lo, hi = school.bookable_range(today)
        if activity_day < lo:
            warnings.append(
                f"活动日期 {activity_day} 距今天不足 {school.MIN_DAYS_AHEAD} 天"
                f"（校方允许的最早日期是 {lo}）"
            )
        elif activity_day > hi:
            warnings.append(
                f"活动日期 {activity_day} 超出提前 {school.MAX_DAYS_AHEAD} 天的上限"
                f"（校方允许的最晚日期是 {hi}）"
            )

    payload = {
        "source": {
            "repo": ctx.settings.repo,
            "doc_id": doc_id,
            "title": str(args.get("doc_title") or ""),
            "author": ctx.author_of(doc_id),
            "dir": ctx.dir_of(doc_id),
            "content_sha256": ctx.sha_of(doc_id),
        },
        # ↓↓↓ 这一段 = crb 的 Activity，字段名与语义完全对齐，可直接进 plan.json
        "activity": {
            "title": title,
            "date": activity_day.isoformat(),
            "period": school.period_label(ksjc, jsjc),
            "people": people,
            "campus": campus_code,
            "building": building_code or None,
            "room_type": None,
            "preferred_room": room_text or None,
        },
        # ↓↓↓ 我们自己的审计轨迹
        "raw": {
            "activity_name": title,
            "date": day_text,
            "start": start,
            "end": end,
            "campus": campus_text,
            "building": building_text,
            "room": room_text,
            "people": int(args.get("people") or 0),
        },
        "derived": {
            "campus_name": school.CAMPUS_CODES.get(campus_code, ""),
            "campus_code": campus_code,
            "ksjc": ksjc,
            "jsjc": jsjc,
            "building_code": building_code,
            "building_note": building_note,
            "people_source": people_source,
        },
        "agent": {
            "run_id": ctx.run_id,
            "verdict": "accepted",
            "confidence": str(args.get("confidence") or "high"),
            "notes": [str(n) for n in (args.get("notes") or [])],
        },
        "normalizations": normalizations,
        "warnings": warnings,
    }
    try:
        result = outputs.write_application(ctx.settings, payload)
    except ContractError as exc:
        raise ToolError(str(exc)) from exc
    ctx.emitted.append({"type": "application", **result})
    return result


def _emit_notice(ctx: RunContext, args: dict[str, Any]) -> Any:
    """产出一条给社员的通知事件（交给负责 QQ 投递的同学）。

    **能力闸门**：只收 ``LLM_NOTICE_KINDS``。像 ``plan_updated`` 那种由程序自己发的
    类型，LLM 根本传不进来——不是靠提示词叮嘱它别发。
    """
    kind = str(args.get("kind") or "")
    if kind not in outputs.LLM_NOTICE_KINDS:
        hint = (
            "（这一类由程序自己发，不需要你产出）" if kind in outputs.PROGRAM_NOTICE_KINDS else ""
        )
        raise ToolError(f"kind 必须是 {list(outputs.LLM_NOTICE_KINDS)} 之一，收到 {kind!r}{hint}")
    message = str(args.get("message") or "")
    payload = {
        "doc": {
            "doc_id": int(args.get("doc_id") or 0),
            "title": str(args.get("doc_title") or ""),
            "url": _doc_url(ctx, int(args.get("doc_id") or 0)),
        },
        "member": {"name": str(args.get("member_name") or "")},
        "summary": str(args.get("summary") or ""),
        "message": message,
        "reasons": [str(r) for r in (args.get("reasons") or [])],
        "warnings": [str(w) for w in (args.get("warnings") or [])],
    }
    try:
        result = outputs.write_notice(ctx.settings, kind=kind, payload=payload)
    except ContractError as exc:
        raise ToolError(str(exc)) from exc
    ctx.emitted.append({"type": "notice", **result})
    return result


def _done(ctx: RunContext, args: dict[str, Any]) -> Any:
    """本轮结束。给出一个判定标签与一句话摘要（会写进 session 与工作日志）。"""
    ctx.verdict = str(args.get("verdict") or "unspecified")
    ctx.summary = str(args.get("summary") or "")
    ctx.finished = True
    return {
        "verdict": ctx.verdict,
        "summary": ctx.summary,
        "emitted": len(ctx.emitted),
        "kb_writes": len(ctx.kb_writes),
    }


# ---------------------------------------------------------------- 归档工具（仅 archive）


def _toc_create(ctx: RunContext, args: dict[str, Any]) -> Any:
    """在知识库目录里新建一个分组目录（例如下一周的申请目录）。"""
    title = str(args.get("title") or "").strip()
    if not title:
        raise ToolError("title 必填")
    target_uuid = str(args.get("target_uuid") or "")
    if ctx.settings.dry_run:
        ctx.note_kb_write("toc_create", {"title": title, "target_uuid": target_uuid})
        return {"dry_run": True, "would_create": title}
    ctx.client.toc_add(title=title, target_uuid=target_uuid)
    ctx.client.wait_toc_settled()
    ctx.note_kb_write("toc_create", {"title": title, "target_uuid": target_uuid})
    return {"created": title, "note": "语雀目录有秒级写入延迟，稍后再读才能看到"}


def _toc_move(ctx: RunContext, args: dict[str, Any]) -> Any:
    """把一个目录节点移动到另一个目录下（例如把上一周期的目录移进归档区）。"""
    node_uuid = _need(args, "node_uuid")
    target_uuid = str(args.get("target_uuid") or "")
    if ctx.settings.dry_run:
        ctx.note_kb_write("toc_move", {"node_uuid": node_uuid, "target_uuid": target_uuid})
        return {"dry_run": True, "would_move": node_uuid}
    ctx.client.toc_move(node_uuid=node_uuid, target_uuid=target_uuid)
    ctx.client.wait_toc_settled()
    ctx.note_kb_write("toc_move", {"node_uuid": node_uuid, "target_uuid": target_uuid})
    return {"moved": node_uuid, "target_uuid": target_uuid or "(根目录末尾)"}


def _toc_remove(ctx: RunContext, args: dict[str, Any]) -> Any:
    """把一个节点从目录里摘掉（**不删文档**）。"""
    node_uuid = _need(args, "node_uuid")
    with_children = bool(args.get("with_children"))
    if ctx.settings.dry_run:
        ctx.note_kb_write("toc_remove", {"node_uuid": node_uuid, "with_children": with_children})
        return {"dry_run": True, "would_remove": node_uuid}
    ctx.client.toc_remove(node_uuid=node_uuid, with_children=with_children)
    ctx.client.wait_toc_settled()
    ctx.note_kb_write("toc_remove", {"node_uuid": node_uuid, "with_children": with_children})
    return {"removed_from_toc": node_uuid}


def _doc_create(ctx: RunContext, args: dict[str, Any]) -> Any:
    """在知识库里新建一篇文档（例如重建被人删掉的《指导文档》）。"""
    title = _need(args, "title")
    body = str(args.get("body") or "")
    if ctx.settings.dry_run:
        ctx.note_kb_write("doc_create", {"title": title})
        return {"dry_run": True, "would_create_doc": title}
    created = ctx.client.create_doc(title=title, body=body)
    doc_id = int((created or {}).get("id") or 0)
    parent = str(args.get("parent_uuid") or "")
    if doc_id:
        ctx.client.toc_add(doc_ids=[doc_id], target_uuid=parent)
        ctx.client.wait_toc_settled()
    ctx.note_kb_write("doc_create", {"title": title, "doc_id": doc_id})
    return {"doc_id": doc_id, "title": title}


def _doc_delete(ctx: RunContext, args: dict[str, Any]) -> Any:
    """删除一篇文档。**只在归档会话里可用**，且必须写明理由。"""
    doc_id = _need(args, "doc_id")
    reason = str(args.get("reason") or "").strip()
    if not reason:
        raise ToolError("reason 必填——删文档必须说明理由，理由会写进 session 与工作日志")
    if ctx.settings.dry_run:
        ctx.note_kb_write("doc_delete", {"doc_id": doc_id, "reason": reason})
        return {"dry_run": True, "would_delete": doc_id, "reason": reason}
    ctx.client.delete_doc(doc_id)
    ctx.note_kb_write("doc_delete", {"doc_id": doc_id, "reason": reason})
    return {"deleted": doc_id, "reason": reason}


# ---------------------------------------------------------------- 注册表


COMMON_TOOLS: tuple[Tool, ...] = (
    Tool(
        "kb_tree",
        "查看知识库的目录树（每个目录下有几篇文档）。用来判断一篇文档应该在哪、"
        "或者知识库结构是不是被人改乱了。",
        _params({}),
        _kb_tree,
    ),
    Tool(
        "dir_list",
        "列出某个目录下的全部文档（不只是本轮变过的）。需要知道「同一时间段还有谁申请了教室」时用。",
        _params(
            {
                "dir": {
                    **STR,
                    "description": "目录路径，如 '0919-0925' 或 '归档区/0912-0918'；根目录用 '.'",
                }
            },
            ["dir"],
        ),
        _dir_list,
    ),
    Tool(
        "doc_read",
        "读一篇文档的完整正文（markdown）。变更报告里的 preview 被截断或不够判断时用它。",
        _params({"doc": {**STR, "description": "doc_id 或 slug"}}, ["doc"]),
        _doc_read,
    ),
    Tool(
        "ws_list",
        "列工作区里的文件。可以看自己上一轮产出了什么（outbox/applications、outbox/notify）"
        "以及在 notes/ 里留下的记忆。",
        _params({"path": {**STR, "description": "相对工作区的路径，默认 '.'(根)"}}),
        _ws_list,
    ),
    Tool("ws_read", "读工作区里的一个文件。", _params({"path": STR}, ["path"]), _ws_read),
    Tool(
        "ws_write",
        "在 notes/ 下写一个文件作为跨轮记忆（例如「哪些 doc_id 已经受理过」）。"
        "只能写到 notes/ 里，别的地方会报错。",
        _params({"path": STR, "content": STR}, ["path", "content"]),
        _ws_write,
    ),
    Tool(
        "emit_application",
        "产出一份要素齐备的教室借用申请，交给负责提交的同学。"
        "请填**社员原话**（校区写「仙林」、教学楼写「仙II区」）——"
        "程序会自动把它们转成学校系统需要的代码，并在对不上时把错误告诉你。"
        "无法确定的（教学楼 / 教室 / 人数）就留空，空 = 随机分配。",
        _params(
            {
                "doc_id": INT,
                "doc_title": STR,
                "activity_name": {**STR, "description": "活动名称，一般就用文档标题"},
                "activity_date": {**STR, "description": "YYYY-MM-DD"},
                "start": {**STR, "description": "开始时刻 HH:MM，24 小时制"},
                "end": {**STR, "description": "结束时刻 HH:MM，24 小时制"},
                "campus": {**STR, "description": "鼓楼 / 浦口 / 仙林 / 苏州"},
                "building": {**STR, "description": "教学楼，社员怎么写的就怎么填；不确定就留空"},
                "room": {**STR, "description": "意向教室，原样透传；不确定就留空"},
                "people": {**INT, "description": "人数；没写就传 0，程序按 30 计"},
                "confidence": {**STR, "description": "high / medium / low"},
                "normalizations": {**STRLIST, "description": "你做过哪些规范化（供人复核）"},
                "warnings": {**STRLIST, "description": "可疑但放行的提醒"},
                "notes": {**STRLIST, "description": "给负责提交的同学看的备注"},
            },
            ["doc_id", "activity_date", "campus", "start", "end"],
        ),
        _emit_application,
    ),
    Tool(
        "emit_notice",
        "产出一条通知事件（由另一位同学通过 QQ 投递给社员）。"
        "message 必须是可以直接发出去的中文正文。",
        _params(
            {
                "kind": {
                    **STR,
                    # 从契约推导，不手抄——手抄的枚举是另一个会漂的东西。
                    "description": " / ".join(outputs.LLM_NOTICE_KINDS),
                },
                "summary": {**STR, "description": "一句话标题"},
                "message": {**STR, "description": "给社员看的完整中文正文"},
                "doc_id": INT,
                "doc_title": STR,
                "member_name": {
                    **STR,
                    "description": (
                        "可选：文档里「申请人：」后面那个人名，**照抄别改**。"
                        "程序拿它查表决定这条通知发给谁（某个社员的 QQ，或某个群）；"
                        "查不到或留空都会走兜底目标，通知照样发得出去。"
                        "没写申请人就留空，不要编名字，也不要让社员去补写这一行"
                        "（受理后文档已锁定，他一改会被判「改动无效」）。"
                    ),
                },
                "reasons": STRLIST,
                "warnings": STRLIST,
            },
            ["kind", "summary", "message"],
        ),
        _emit_notice,
    ),
    Tool(
        "done",
        "本轮工作结束。verdict 是一个简短的判定标签，summary 是一句话结论。",
        _params(
            {
                "verdict": {
                    **STR,
                    "description": "如 skipped_draft / accepted / rejected / unrecognized / "
                    "tampered / deleted / structure_fixed / nothing_to_do",
                },
                "summary": STR,
            },
            ["verdict", "summary"],
        ),
        _done,
    ),
)


ARCHIVE_TOOLS: tuple[Tool, ...] = (
    Tool(
        "toc_create",
        "在知识库目录里新建一个分组目录（例如下一周的申请目录）。",
        _params(
            {"title": STR, "target_uuid": {**STR, "description": "父节点 uuid，留空=根目录"}},
            ["title"],
        ),
        _toc_create,
    ),
    Tool(
        "toc_move",
        "把一个目录节点移到另一个目录下（例如把上一周的目录移进归档区）。",
        _params(
            {
                "node_uuid": STR,
                "target_uuid": {**STR, "description": "目标父节点 uuid，留空=移到根目录末尾"},
            },
            ["node_uuid"],
        ),
        _toc_move,
    ),
    Tool(
        "toc_remove",
        "把一个节点从目录里摘掉（不会删除文档本身）。",
        _params({"node_uuid": STR, "with_children": BOOL}, ["node_uuid"]),
        _toc_remove,
    ),
    Tool(
        "doc_create",
        "在知识库里新建一篇文档。",
        _params({"title": STR, "body": STR, "parent_uuid": STR}, ["title"]),
        _doc_create,
    ),
    Tool(
        "doc_delete",
        "删除一篇文档。必须填写 reason，理由会被留档。",
        _params(
            {"doc_id": INT, "reason": {**STR, "description": "为什么必须删掉它"}},
            ["doc_id", "reason"],
        ),
        _doc_delete,
    ),
)


def tools_for(kind: str) -> tuple[Tool, ...]:
    """本轮会话注册哪些工具——**这就是安全边界的全部**。"""
    if kind == "archive":
        return COMMON_TOOLS + ARCHIVE_TOOLS
    return COMMON_TOOLS


def tool_schemas(kind: str) -> list[dict[str, Any]]:
    return [tool.schema() for tool in tools_for(kind)]


def tool_names(kind: str) -> list[str]:
    return [tool.name for tool in tools_for(kind)]


def execute(ctx: RunContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """执行一次工具调用。**任何失败都变成返回值**，不中断 agent loop。"""
    registry = {tool.name: tool for tool in tools_for(ctx.kind)}
    tool = registry.get(name)
    if tool is None:
        return {
            "ok": False,
            "error": f"本轮没有注册名为 {name!r} 的工具。本轮可用：{sorted(registry)}",
        }

    watch = Stopwatch()
    try:
        result = tool.handler(ctx, args)
        return {"ok": True, "result": result, "ms": watch.ms()}
    except (ToolError, ContractError, YuqueError) as exc:
        return {"ok": False, "error": str(exc), "ms": watch.ms()}
    except Exception as exc:  # noqa: BLE001 - 兜底：绝不让单个工具搞崩整轮
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "ms": watch.ms()}


# ---------------------------------------------------------------- 小工具


def _need(args: dict[str, Any], key: str) -> Any:
    value = args.get(key)
    if value in (None, "", 0):
        raise ToolError(f"{key} 必填")
    return value


def _doc_url(ctx: RunContext, doc_id: int) -> str:
    if not doc_id:
        return ""
    host = ctx.settings.host.rstrip("/")
    for doc in ctx.docs.values():
        if doc.doc_id == doc_id and doc.slug:
            return f"{host}/{ctx.settings.repo}/{doc.slug}"
    return f"{host}/{ctx.settings.repo}"
