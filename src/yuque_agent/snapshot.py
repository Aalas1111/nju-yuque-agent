"""快照与变更检测。

**这是程序侧唯一有「智力」的地方，而它做的事只有一件：算出「哪些东西变了」。**

它不做、也永远不该做的判定：

* 不判断文档是不是草稿（「草稿标签删了一半」程序判不了，LLM 能判）；
* 不判断字段填得对不对、时间合不合规；
* 不判断文档该不该被处理。

程序给 LLM 的输入是**纯客观事实**：谁在什么时候，在哪，改了什么。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any

from . import clock
from .yuque import DocMeta, TocNode, YuqueClient, YuqueError, doc_dir_map

PREVIEW_CHARS = 300


# ---------------------------------------------------------------- 数据


@dataclass(frozen=True)
class DocSnapshot:
    doc_id: int
    slug: str
    title: str
    updated_at: str
    created_at: str
    author: str
    dir: str
    """所在目录（人类可读路径，如 ``0919-0925`` / ``归档区/0912-0918``）。"""
    content_sha256: str = ""
    """正文指纹。**惰性填充**：只在「该文档本次变了」时才去读正文算一次。

    为什么需要它：**语雀在文档的目录位置发生变化时也会 bump ``updated_at``**
    （实测：把周期目录 move 一下，里面所有文档的 ``updated_at`` 都会变）。
    只用 ``updated_at`` 做 diff 会把「只动了目录」误报成「文档被改了」，
    进而给社员发一条莫名其妙的「你改了文档」通知。

    所以程序分两步：``updated_at`` 变了 → **才**读正文算哈希 → 哈希也没变就丢掉这条变更。
    「内容到底变没变」是**测量**，不是判断，所以它归程序。
    """

    @property
    def fingerprint(self) -> str:
        return f"{self.title}\x00{self.updated_at}\x00{self.dir}"


@dataclass
class Snapshot:
    taken_at: str
    docs: dict[int, DocSnapshot] = field(default_factory=dict)
    toc: list[dict[str, Any]] = field(default_factory=list)
    toc_sha: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "taken_at": self.taken_at,
            "toc_sha": self.toc_sha,
            "toc": self.toc,
            "docs": {str(k): asdict(v) for k, v in self.docs.items()},
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Snapshot:
        docs = {
            int(k): DocSnapshot(**v)
            for k, v in (payload.get("docs") or {}).items()
            if isinstance(v, dict)
        }
        return cls(
            taken_at=str(payload.get("taken_at") or ""),
            docs=docs,
            toc=list(payload.get("toc") or []),
            toc_sha=str(payload.get("toc_sha") or ""),
        )


@dataclass
class Changes:
    added: list[DocSnapshot] = field(default_factory=list)
    updated: list[tuple[DocSnapshot, DocSnapshot]] = field(default_factory=list)
    """(旧, 新)"""
    removed: list[DocSnapshot] = field(default_factory=list)
    toc_changed: bool = False
    first_run: bool = False
    toc_only: list[DocSnapshot] = field(default_factory=list)
    """``updated_at`` 变了但**正文哈希没变**的文档（只被挪了目录）——不算变更。"""

    @property
    def n_docs(self) -> int:
        return len(self.added) + len(self.updated) + len(self.removed)

    @property
    def empty(self) -> bool:
        """**是否真的无事可做**。只按「文档级变更」判，**不算目录结构变化**。

        为什么不把 ``toc_changed`` 算进去：**新建一篇文档必然会在目录里加一个节点**，
        于是「文档内容没有实质变化」的判定会被目录变化抵消掉
        （占位标题过滤、正文哈希过滤都因此失效）。

        而目录层面的真实变化其实已经在文档级反映出来了：
        一篇文档被挪了位置 → 它的 ``dir`` 变了 → 算 ``updated``。
        纯目录变化（建了个空分组、调了下顺序）信号极低，
        交给每周六的归档会话去管就好。
        """
        return self.n_docs == 0


# ---------------------------------------------------------------- 采集


def take_snapshot(client: YuqueClient, *, now: datetime | None = None) -> Snapshot:
    """拉一次全量快照（目录 + 文档列表）。共 2 类请求 + 分页。"""
    stamp = (now or clock.now()).isoformat()
    toc_nodes: list[TocNode] = client.toc()
    metas: list[DocMeta] = client.docs()

    doc_dir = doc_dir_map(toc_nodes)
    docs = {
        meta.doc_id: DocSnapshot(
            doc_id=meta.doc_id,
            slug=meta.slug,
            title=meta.title,
            updated_at=meta.updated_at,
            created_at=meta.created_at,
            author=meta.author,
            dir=doc_dir.get(meta.doc_id, ""),
        )
        for meta in metas
        if meta.doc_id
    }
    toc_payload = [
        {
            "uuid": n.uuid,
            "type": n.type,
            "title": n.title,
            "depth": n.depth,
            "path": n.path,
            # 带上父子关系：算「文档在哪个目录」必须用它，**不能去切 ``path`` 字符串**。
            # 实测踩到过：有人的文档标题里带 ``/``（「测试1（我不申请了/(ㄒoㄒ)/~~）」），
            # 按 ``path.split('/')`` 切出来的目录就是垃圾，于是工具把这篇文档
            # 当成不在任何目录里。
            "parent_uuid": n.parent_uuid,
            "doc_id": n.doc_id,
        }
        for n in toc_nodes
    ]
    return Snapshot(
        taken_at=stamp,
        docs=docs,
        toc=toc_payload,
        toc_sha=_sha(json.dumps([(n["uuid"], n["title"], n["depth"]) for n in toc_payload])),
    )


def compute_changes(prev: Snapshot | None, cur: Snapshot) -> Changes:
    """纯函数：两个快照 → 变更集合（**第一道筛子，只看 updated_at**）。

    真正的「内容有没有变」由 :func:`enrich_and_refine` 用正文哈希确认。
    """
    if prev is None or not prev.taken_at:
        return Changes(first_run=True)

    added: list[DocSnapshot] = []
    updated: list[tuple[DocSnapshot, DocSnapshot]] = []
    for doc_id, new in cur.docs.items():
        old = prev.docs.get(doc_id)
        if old is None:
            added.append(new)
        elif old.fingerprint != new.fingerprint:
            updated.append((old, new))

    removed = [old for doc_id, old in prev.docs.items() if doc_id not in cur.docs]

    return Changes(
        added=sorted(added, key=lambda d: d.doc_id),
        updated=sorted(updated, key=lambda pair: pair[1].doc_id),
        removed=sorted(removed, key=lambda d: d.doc_id),
        toc_changed=prev.toc_sha != cur.toc_sha,
    )


# ---------------------------------------------------------------- 变更报告


def build_report(
    *,
    run_id: str,
    kind: str,
    client: YuqueClient,
    repo: str,
    cur: Snapshot,
    changes: Changes,
    max_docs: int = 50,
    previews: dict[int, str] | None = None,
) -> dict[str, Any]:
    """组装给 LLM 的「变更报告」。

    ``previews`` 是 :func:`enrich_and_refine` 已经读过的正文（避免重复读）。
    读不到正文不影响整轮（降级为空串并记 note）。
    """
    notes: list[str] = []
    cache = previews or {}
    budget = max_docs
    truncated = False

    def take_budget(n: int) -> int:
        nonlocal budget, truncated
        allowed = max(0, min(n, budget))
        if allowed < n:
            truncated = True
        budget -= allowed
        return allowed

    def preview_of(doc_id: int) -> str:
        if doc_id in cache:
            return _preview(cache[doc_id])
        try:
            detail = client.doc(doc_id)
        except YuqueError as exc:
            notes.append(f"读取 doc_id={doc_id} 正文失败：{exc}")
            return ""
        return _preview(detail.body)

    added_payload = []
    for doc in changes.added[: take_budget(len(changes.added))]:
        added_payload.append({**_doc_payload(doc), "preview": preview_of(doc.doc_id)})

    updated_payload = []
    for old, new in changes.updated[: take_budget(len(changes.updated))]:
        updated_payload.append(
            {
                **_doc_payload(new),
                "prev_title": old.title,
                "prev_updated_at": old.updated_at,
                "prev_dir": old.dir,
                "preview": preview_of(new.doc_id),
            }
        )

    # removed 不需要正文，不占预算
    removed_payload = [
        _doc_payload(doc) | {"last_seen_at": cur.taken_at} for doc in changes.removed
    ]

    if truncated:
        notes.append(
            f"本轮变更文档数超过 max_docs={max_docs}，报告只列出了一部分；"
            "如需处理其余文档，请用 dir_list / doc_read 自行查看。"
        )
    if changes.toc_only:
        names = "、".join(d.title for d in changes.toc_only[:10])
        notes.append(
            f"另外有 {len(changes.toc_only)} 篇文档的 updated_at 变了但**正文一个字都没变**"
            f"（只是被挪了目录），已视为无变更、不列入上面：{names}"
        )

    return {
        "run_id": run_id,
        "kind": kind,
        "at": cur.taken_at,
        "repo": {"namespace": repo, "toc_sha": cur.toc_sha},
        "first_run": changes.first_run,
        "toc_changed": changes.toc_changed,
        "toc": cur.toc,
        "docs": {
            "added": added_payload,
            "updated": updated_payload,
            "removed": removed_payload,
        },
        "counts": {
            "added": len(changes.added),
            "updated": len(changes.updated),
            "removed": len(changes.removed),
        },
        "notes": notes,
        "toc_only": [{"doc_id": d.doc_id, "title": d.title} for d in changes.toc_only],
    }


def enrich_and_refine(
    client: YuqueClient,
    prev: Snapshot | None,
    cur: Snapshot,
    changes: Changes,
    *,
    max_reads: int = 50,
) -> dict[int, str]:
    """用**正文哈希**确认「到底有没有改内容」，顺便把正文缓存给报告复用。

    两件事：

    1. 未变化的文档：把上一轮的哈希带过来（保持快照「温热」）；
    2. 本轮变化的文档：读一次正文，算 ``sha256(title + body)``；
       ``updated_at`` 变了但哈希没变（只被挪了目录）→ 从 ``updated`` 里拎出去，
       记进 ``changes.toc_only``。

    返回 ``doc_id -> 正文``，供 :func:`build_report` 生成 preview，避免重复读。
    """
    previews: dict[int, str] = {}
    budget = max_reads

    if prev is None:
        # 基线轮：把全部文档的正文哈希预热一遍。
        # 多花 N 次读，换来「以后每一次 updated_at 变动都能被准确甄别」——
        # 否则第一次变动时无参照，只能保守地当成真变更（可能就是一次误报）。
        for doc in cur.docs.values():
            if budget <= 0:
                break
            body = _read_body(client, doc.doc_id)
            if body is None:
                continue
            budget -= 1
            cur.docs[doc.doc_id] = replace(doc, content_sha256=_content_sha(doc.title, body))
    else:
        for doc_id, doc in cur.docs.items():
            if doc.content_sha256:
                continue
            old = prev.docs.get(doc_id)
            if old is None or not old.content_sha256:
                continue
            if old.updated_at == doc.updated_at and old.title == doc.title:
                cur.docs[doc_id] = replace(doc, content_sha256=old.content_sha256)

    for doc in changes.added:
        if budget <= 0:
            break
        body = _read_body(client, doc.doc_id)
        if body is None:
            continue
        budget -= 1
        previews[doc.doc_id] = body
        cur.docs[doc.doc_id] = replace(doc, content_sha256=_content_sha(doc.title, body))

    kept: list[tuple[DocSnapshot, DocSnapshot]] = []
    for old, new in changes.updated:
        if budget <= 0:
            kept.append((old, new))
            continue
        body = _read_body(client, new.doc_id)
        if body is None:
            kept.append((old, new))
            continue
        budget -= 1
        sha = _content_sha(new.title, body)
        previews[new.doc_id] = body
        cur.docs[new.doc_id] = replace(new, content_sha256=sha)
        if old.content_sha256 and sha == old.content_sha256:
            # 内容一个字都没变（典型原因：文档被挪了目录）→ 不是变更，不叫醒 LLM
            changes.toc_only.append(new)
        else:
            kept.append((old, new))
    changes.updated = kept
    return previews


def _read_body(client: YuqueClient, doc_id: int) -> str | None:
    try:
        return client.doc(doc_id).body
    except YuqueError:
        return None


def drop_placeholders(
    changes: Changes,
    previews: dict[int, str],
    placeholder_titles: tuple[str, ...] | list[str],
) -> list[DocSnapshot]:
    """把「**标题还是语雀的占位标题、且正文为空**」的中间态从变更里剔除。

    为什么要这一层：语雀新建文档时标题默认是「无标题」/「无标题文档」——
    这**不是社员写的**，是机器生成的占位符，不可能携带任何借用意图。
    它比草稿标记还要早一步：草稿标记至少是人主动删的，占位标题连人都没碰过。

    **为什么还要加「且正文为空」这个条件**：如果社员手快、直接点进正文粘贴内容而没改标题，
    那他就是真的想申请。只看标题会把他静默丢掉——这是本项目最不能接受的失败模式。
    正文一有内容就不是中间态，照旧交给 LLM（它会告诉他「标题得写活动名」）。

    安全取值：**正文读不到时不做剔除**（宁可白跑一轮，也不漏掉真实申请）。

    返回被剔除的文档（用于日志/调试）。
    """
    titles = set(placeholder_titles or ())
    if not titles:
        return []
    dropped: list[DocSnapshot] = []

    def blank_placeholder(doc: DocSnapshot) -> bool:
        if doc.title not in titles:
            return False
        body = previews.get(doc.doc_id)
        return body is not None and not body.strip()

    kept_added: list[DocSnapshot] = []
    for doc in changes.added:
        if blank_placeholder(doc):
            dropped.append(doc)
        else:
            kept_added.append(doc)
    changes.added = kept_added

    kept_updated: list[tuple[DocSnapshot, DocSnapshot]] = []
    for old, new in changes.updated:
        if blank_placeholder(new):
            dropped.append(new)
        else:
            kept_updated.append((old, new))
    changes.updated = kept_updated

    # 删除只能看**上一次看到的标题**（正文已经读不到了）。
    # 一篇从未有过真标题的文档被删掉，不是信号——它从来没携过任何意图。
    changes.removed = [doc for doc in changes.removed if doc.title not in titles]

    return dropped


def drop_archived(changes: Changes, archive_title: str) -> list[DocSnapshot]:
    """把**在归档区里**的文档从变更里剔除（返回被剔除的）。

    为什么要这一层（2026-09-27 负责人拍板）：归档区是终点站——那里的改动，
    LLM 唯一的正确动作是「什么都不做」（提示词写着「一律不处理、不通知」）。
    既然它无事可做，就不该为它花 token 叫醒一次；结构性的问题留给每周六的
    归档会话（它另有一条完整目录树，不看这里的变更列表）。

    为什么归程序：判断只需要一个**事实**——文档所在的目录（由 ``parent_uuid`` 算出的
    ``dir``，和「文档在哪个目录」是同一份算法）。这和占位标题那道筛子同类：
    机器可测的前置条件，不是语义判断。目录节点被改名时这条筛子会失效
    （那时又回到 LLM 判断），提示词里那条规矩仍在，兜得住。
    """
    if not archive_title:
        return []

    def in_archive(doc: DocSnapshot) -> bool:
        where = doc.dir or ""
        return where == archive_title or where.startswith(f"{archive_title}/")

    dropped: list[DocSnapshot] = []

    kept_added: list[DocSnapshot] = []
    for doc in changes.added:
        if in_archive(doc):
            dropped.append(doc)
        else:
            kept_added.append(doc)
    changes.added = kept_added

    kept_updated: list[tuple[DocSnapshot, DocSnapshot]] = []
    for old, new in changes.updated:
        if in_archive(new):
            dropped.append(new)
        else:
            kept_updated.append((old, new))
    changes.updated = kept_updated

    kept_removed: list[DocSnapshot] = []
    for doc in changes.removed:
        if in_archive(doc):
            dropped.append(doc)
        else:
            kept_removed.append(doc)
    changes.removed = kept_removed

    kept_toc_only: list[DocSnapshot] = []
    for doc in changes.toc_only:
        if in_archive(doc):
            dropped.append(doc)
        else:
            kept_toc_only.append(doc)
    changes.toc_only = kept_toc_only
    return dropped


def _content_sha(title: str, body: str) -> str:
    """标题也算内容——标题就是活动名称，改名是实质变更。"""
    return hashlib.sha256(f"{title}\x00{body.strip()}".encode()).hexdigest()


def _doc_payload(doc: DocSnapshot) -> dict[str, Any]:
    return {
        "doc_id": doc.doc_id,
        "slug": doc.slug,
        "title": doc.title,
        "dir": doc.dir,
        "author": doc.author,
        "created_at": doc.created_at,
        "updated_at": doc.updated_at,
    }


def _preview(body: str, limit: int = PREVIEW_CHARS) -> str:
    text = (body or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（已截断，全文共 {len(text)} 字，需要全文请调 doc_read）"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
