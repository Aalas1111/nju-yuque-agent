"""编排层：把「快照 → diff → 唤醒 LLM → 留痕」串成一轮。

程序在这里做的判定**只有一条**：*这一轮到底有没有变化*。
有变化才叫 LLM（省 token 是顺带的，真正目的是让「无变化」成为一个可证明的静默状态）。
除此之外，它不给 LLM 任何提示性的结论。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import clock, outputs
from . import journal as journal_mod
from .agent import RunResult, run_agent, session_path_for
from .config import Settings
from .llm import LLMClient
from .prompts import PromptLoader
from .session import SessionRecorder
from .snapshot import (
    Changes,
    DocSnapshot,
    Snapshot,
    build_report,
    compute_changes,
    drop_placeholders,
    enrich_and_refine,
    take_snapshot,
)
from .tools import RunContext
from .week import cycle_of as week_cycle_of
from .week import cycle_targets
from .yuque import YuqueClient

STATE_VERSION = 1


@dataclass
class State:
    """程序侧记忆：上一轮快照 + 归档水位线 + 静默期计时。**LLM 看不到它，也不需要看到。**"""

    snapshot: Snapshot | None = None
    rounds: int = 0
    last_poll_at: str = ""
    last_archive_title: str = ""
    last_archive_at: str = ""

    pending_since: str = ""
    """首次检测到变化的时间（ISO）。非空 = 有一批变更正在等静默期结束。"""
    pending_polls: int = 0
    """这批变更已经攒了多少轮（用于在报告里展示「合并掉了多少次唤醒」）。"""
    journal_doc_id: int = 0
    placeholder_dropped: int = 0
    """上一轮被剔除的「占位标题 + 空正文」文档数（仅用于自检/调试）。"""

    active_cycle: str = ""
    """``outbox/applications/`` 里那批申请属于哪个周期（``0919-0925``）。

    程序靠它判断「周期是不是翻了」→ 该不该把产物搬进 ``archive/``。
    空 = 还没记过（升级前的部署），此时从申请文件自带的 ``cycle`` 字段推断，
    见 :func:`outputs.detect_active_cycle`。
    """

    plan_notified: str = ""
    """最近一次**已经通知过管理员**的清单指纹（见 :func:`outputs.plan_fingerprint`）。

    作用是不重发：一轮里写了 3 份申请，管理员只该收到 **1** 条「请下单」；
    周期翻转后清单变空，也不该发通知（不打扰人）。
    """
    """《工作日志》的 doc_id。程序自己写的文档，永远不当变更信号（见 Settings.ignore_doc_titles）。"""

    def to_json(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "rounds": self.rounds,
            "last_poll_at": self.last_poll_at,
            "last_archive_title": self.last_archive_title,
            "last_archive_at": self.last_archive_at,
            "pending_since": self.pending_since,
            "pending_polls": self.pending_polls,
            "journal_doc_id": self.journal_doc_id,
            "placeholder_dropped": self.placeholder_dropped,
            "active_cycle": self.active_cycle,
            "plan_notified": self.plan_notified,
            "snapshot": self.snapshot.to_json() if self.snapshot else None,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> State:
        snap = payload.get("snapshot")
        return cls(
            snapshot=Snapshot.from_json(snap) if isinstance(snap, dict) else None,
            rounds=int(payload.get("rounds") or 0),
            last_poll_at=str(payload.get("last_poll_at") or ""),
            last_archive_title=str(payload.get("last_archive_title") or ""),
            last_archive_at=str(payload.get("last_archive_at") or ""),
            pending_since=str(payload.get("pending_since") or ""),
            pending_polls=int(payload.get("pending_polls") or 0),
            journal_doc_id=int(payload.get("journal_doc_id") or 0),
            placeholder_dropped=int(payload.get("placeholder_dropped") or 0),
            active_cycle=str(payload.get("active_cycle") or ""),
            plan_notified=str(payload.get("plan_notified") or ""),
        )


def load_state(path: Path) -> State:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return State()
    return State.from_json(payload) if isinstance(payload, dict) else State()


def save_state(state: State, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def new_run_id(kind: str, *, at: datetime | None = None) -> str:
    stamp = clock.compact_stamp(at)
    return f"{stamp}-{kind}-{secrets.token_hex(2)}"


def _seconds_since(stamp: str, now: datetime) -> float | None:
    """``stamp``（ISO）到 ``now`` 的秒数；解析不了返回 ``None``。"""
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return None


def _day_of(payload: dict[str, Any]) -> date | None:
    """从变更报告的 ``at`` 里取出「今天」（用于校方借用日期范围的提醒）。"""
    raw = str(payload.get("at") or "")
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


@dataclass
class Runner:
    settings: Settings
    client: YuqueClient
    llm: LLMClient
    prompt: PromptLoader = field(default_factory=PromptLoader)
    _state: State | None = field(default=None, repr=False)

    #: 上一次 :meth:`poll_once` 为什么没唤醒 LLM，取值 ``"no_change"`` / ``"quiet_period"``，
    #: 真的跑了就是空串。
    #:
    #: 为什么要有这个字段：``poll_once`` 返回 ``None`` 有**两种**原因——「没变化」和
    #: 「还在静默期」。调用方如果一律说「知识库没有变化」，就会在静默期里说假话。
    #: 实测踩到过：写完文档立刻 ``yqa once``，被报「没有变化」，让人以为程序坏了。
    last_skip: str = ""

    #: 上一轮 :meth:`rotate_cycle_if_needed` 真搬了东西时的结果（否则 ``None``）。
    #: 只是给人看的（日志 / 报告注释）——周期翻转本身不花 token，也不依赖 LLM。
    last_rotation: dict[str, Any] | None = None

    # -- 状态 -------------------------------------------------------------
    @property
    def state(self) -> State:
        if self._state is None:
            self._state = load_state(self.settings.state_file)
        return self._state

    def save_state(self) -> None:
        save_state(self.state, self.settings.state_file)

    # -- 轮询入口 ---------------------------------------------------------
    def snapshot_now(self) -> Snapshot:
        """拿一次快照，并**把程序自己写的文档剔除掉**。

        为什么必须剔除：《工作日志》就在被监控的知识库里。不剔的话：
        程序写日志 → 日志变了 → 唤醒 LLM → 又写日志 → …（自激循环，实测踩到过）。
        """
        snapshot = take_snapshot(self.client)
        ignored = self._ignored_doc_ids(snapshot)
        if ignored:
            snapshot.docs = {
                doc_id: doc for doc_id, doc in snapshot.docs.items() if doc_id not in ignored
            }
        return snapshot

    # -- 周期翻转：把上个周期的产物搬进 archive/ -------------------------
    def rotate_cycle_if_needed(self, moment: datetime) -> dict[str, Any] | None:
        """周期翻了就把 ``outbox/`` 的活跃产物整批搬进 ``archive/<上个周期>/``。

        **纯程序、零 token、不依赖 LLM 的归档会话。**

        为什么不交给 LLM：归档会话会失败（网络、模型、token 上限），而
        **产物边界不能跟着它一起失败**——一旦没搬，上个周期的申请就会继续留在
        ``plan.json`` 里，cac 照着提交就是订一个已经过去的日期。
        """
        cycle = week_cycle_of(moment.date(), start_weekday=self.settings.archive_weekday).title
        self.last_rotation = None  # 每轮重置，否则同一次翻转会在后续几轮里反复被报
        if not self.state.active_cycle:
            # 升级前的存量：state 里没记过，从申请文件自带的 cycle 字段推断。
            previous = outputs.detect_active_cycle(self.settings)
            if previous and previous != cycle:
                self.last_rotation = outputs.rotate_outbox(self.settings, cycle=previous)
            self.state.active_cycle = cycle
            self.save_state()
            return self.last_rotation

        if self.state.active_cycle == cycle:
            return None

        self.last_rotation = outputs.rotate_outbox(self.settings, cycle=self.state.active_cycle)
        self.state.active_cycle = cycle
        self.save_state()
        return self.last_rotation

    # -- 交付件变了就提醒管理员 -----------------------------------------
    def notify_plan_updated_if_changed(self) -> dict[str, Any] | None:
        """清单**真变了**才给管理员发一条「请下载提交」。

        为什么归程序而不是 LLM：LLM 只看得到「这一轮改了哪几篇文档」，
        看不到清单整体长什么样、有没有过期。而「清单变了」这个事实是可测的
        （扫目录 + 算指纹），所以归程序——而且不会因为 LLM 忘了而漏发。

        去重按**内容指纹**而不是时间戳：一轮里写 3 份申请只发 1 条，
        周期翻转后清单变空也不发（不打扰人）。
        """
        plan = outputs.build_plan_json(self.settings)
        fingerprint = outputs.plan_fingerprint(plan)
        if fingerprint == self.state.plan_notified:
            return None
        self.state.plan_notified = fingerprint
        self.save_state()
        if not plan.get("activities"):
            return None
        payload = outputs.build_plan_updated_notice(plan, member=self.settings.plan_admin)
        try:
            return outputs.write_notice(self.settings, kind="plan_updated", payload=payload)
        except outputs.ContractError:
            return None

    def _ignored_doc_ids(self, snapshot: Snapshot) -> set[int]:
        ignored: set[int] = set()
        if self.state.journal_doc_id:
            ignored.add(self.state.journal_doc_id)
        titles = set(self.settings.ignore_doc_titles)
        if titles:
            ignored.update(doc_id for doc_id, doc in snapshot.docs.items() if doc.title in titles)
        return ignored

    def detect(self) -> tuple[Snapshot, Changes]:
        """只做感知：拿快照 + 比对。**不唤醒 LLM。**"""
        current = self.snapshot_now()
        changes = compute_changes(self.state.snapshot, current)
        return current, changes

    def poll_once(
        self,
        *,
        force: bool = False,
        rescan: bool = False,
        now: datetime | None = None,
        debounce: bool = True,
        observer: Any = None,
    ) -> RunResult | None:
        """跑一轮轮询。没有变化、或还在静默期内，就返回 ``None``（静默，0 token）。

        返回 ``None`` 时，:attr:`last_skip` 会告诉你**到底是哪种原因**
        （``"no_change"`` / ``"quiet_period"``），调用方不要一律说「没有变化」。

        **静默期合并**（默认开启，``quiet_seconds``）：

        * 发现变化**先不叫 LLM**，并把基线快照摇在那里 —— 这就把后续的连续变更天然合并了；
        * 等知识库安静 ``quiet_seconds`` 秒后，拿**当时的最新状态**一次性处理；
        * 若这期间变更被改回去、或文档被删掉，diff 变空 → **整轮根本不触发**。

        省 token 只是顺带。真正的好处是 **LLM 看到的是最终状态**，而不是语雀手工建文档时
        那一串「无标题空文档 → 有标题无正文 → 有正文」的中间态。

        ``rescan=True``：**无视快照**，把现有全部文档当成「新增」重新评估一遍。
        用于「state 丢了要恢复」或「换了提示词想把存量重跑一遍」。它会重发通知。
        """
        moment = now or clock.now()
        prev = self.state.snapshot
        # 周期翻转：先把上个周期的产物搬进 archive/（纯程序，不花 token，
        # 也不依赖 LLM 的归档会话成不成功）。放在 detect() 之前——
        # 这样本轮报告里看到的 outbox 已经是当前周期的了。
        self.rotate_cycle_if_needed(moment)
        current, changes = self.detect()
        dropped: list[DocSnapshot] = []
        self.last_skip = ""

        if rescan:
            changes = Changes(
                added=sorted(current.docs.values(), key=lambda d: d.doc_id),
                toc_changed=True,
            )
            previews = enrich_and_refine(
                self.client,
                None,  # 不用旧哈希做判断，全部重新读
                current,
                changes,
                max_reads=self.settings.max_doc_reads_per_round,
            )
            changes.updated = []
            changes.toc_only = []
        else:
            # 第二道筛子：用正文哈希确认「到底有没有改内容」。
            # （语雀挪目录也会 bump updated_at；不确认就会给社员误发「你改了文档」）
            previews = enrich_and_refine(
                self.client,
                prev,
                current,
                changes,
                max_reads=self.settings.max_doc_reads_per_round,
            )
            # 第三道筛子：占位标题 + 空正文 = 语雀刚建出来的中间态，**根本不是信号**。
            # 它比草稿标记更早一级：草稿标记至少是人主动删的，占位标题连人都没碰过。
            # 复用上面已经读到的正文，**零额外请求**。
            dropped = drop_placeholders(changes, previews, self.settings.placeholder_titles)
            self.state.placeholder_dropped = len(dropped)

            # ---- 静默期判定：到这里 changes 已经只剩下「真的可能要做点什么」的了 ----
            if changes.empty:
                # 没变化（含「刚建的文档又被删了」「只剩占位标题的空文档」）→ 清空待处理标记
                self.state.pending_since = ""
                self.state.pending_polls = 0
            else:
                if not self.state.pending_since:
                    self.state.pending_since = moment.isoformat(timespec="seconds")
                    self.state.pending_polls = 0
                self.state.pending_polls += 1
                if debounce and self.settings.quiet_seconds > 0:
                    waited = _seconds_since(self.state.pending_since, moment)
                    if waited is not None and waited < self.settings.quiet_seconds:
                        self.state.rounds += 1
                        self.state.last_poll_at = current.taken_at
                        self.save_state()
                        self.last_skip = "quiet_period"
                        return None

        # ---- 走到这里：要么确实没变化，要么该真跑了 ----
        # 只有「这一轮真的要跑」或「确实没变化」时，才推进基线
        collapsed = self.state.pending_polls
        self.state.snapshot = current
        self.state.rounds += 1
        self.state.last_poll_at = current.taken_at
        self.state.pending_since = ""
        self.state.pending_polls = 0
        self.save_state()

        if changes.empty and not force:
            # 注意：**首次运行也是静默的**——那时报告本来就是空的（只建基线），
            # 叫醒 LLM 只会得到一句「无事可做」，纯粹是白烧 token。
            self.last_skip = "no_change"
            return None

        run_id = new_run_id("polling", at=moment)
        report = build_report(
            run_id=run_id,
            kind="polling",
            client=self.client,
            repo=self.settings.repo,
            cur=current,
            changes=changes,
            max_docs=self.settings.max_docs_per_run,
            previews=previews,
        )
        if dropped:
            titles = "、".join(f"「{d.title}」" for d in dropped[:5])
            report["notes"].append(
                f"另有 {len(dropped)} 篇文档是语雀刚建出来的中间态（标题还是占位标题"
                f"且正文为空）：{titles}——程序已把它们从变更里剔除，不需要你处理。"
            )
        if collapsed > 1:
            report["notes"].append(
                f"这轮是**静默期合并**的结果：程序在 {collapsed} 次轮询里都看到了变更，"
                f"但一直等到知识库安静 {self.settings.quiet_seconds} 秒才叫醒你一次，"
                "所以你看到的是**最终状态**。"
                "（语雀手工建文档会分几步产生变更：无标题空文档 → 改标题 → 写正文保存。）"
            )
        if changes.first_run:
            report["notes"].append(
                "本轮是首次运行（基线快照刚建立）。报告里不一定包含全部历史文档；"
                "如果想对存量文档重新评估一遍，请用 `yqa once --rescan`。"
            )
        if rescan:
            report["notes"].append(
                "本轮是 **rescan（重建产物）**：请把下面每篇文档都当作**首次看到**来重新判定，"
                "并且**重新产出** `emit_application` / `emit_notice`。"
                "不要因为历史记录（工作日志 / outbox）里说「已经处理过」就跳过 —— "
                "本轮的**唯一目的就是把产物重新生成一遍**。"
                "（这会导致重复通知，所以 rescan 不是日常命令。）"
            )
        if self.last_rotation and self.last_rotation.get("moved"):
            rot = self.last_rotation
            report["notes"].append(
                f"**周期已翻转**：程序把上个周期（{rot['cycle']}）的产物整批搬进了 "
                f"`outbox/archive/{rot['cycle']}/`（共 {len(rot['moved'])} 个文件）。"
                "这件事是程序做的，不需要你再处理；提一句是为了让你知道 "
                "`outbox/applications/` 为什么变空了。"
            )
        result = self._execute(run_id=run_id, kind="polling", payload=report, observer=observer)
        # 跑完再看一眼清单：这一轮若改了申请，管理员该收到一条「请下载提交」。
        # 放在 _execute 之后，所以一轮里写多少份申请都只提醒一次。
        self.notify_plan_updated_if_changed()
        return result

    # -- 归档入口 ---------------------------------------------------------
    def archive_once(self, *, now: datetime | None = None, observer: Any = None) -> RunResult:
        """时钟驱动：与轮询无关，**即使知识库一个字都没变也会跑**。

        ``now`` 可由调用方注入（常驻循环与测试都传固定时刻，路径才可复现）。
        """
        moment = now or clock.now()
        current = self.snapshot_now()
        payload = build_archive_instruction(
            run_id_prefix="",
            settings=self.settings,
            snapshot=current,
            now=moment,
            guide_body=_guide_body(),
        )
        run_id = new_run_id("archive", at=moment)
        payload["run_id"] = run_id
        result = self._execute(run_id=run_id, kind="archive", payload=payload, observer=observer)

        self.state.last_archive_at = moment.isoformat(timespec="seconds")
        self.state.last_archive_title = str(payload.get("cycle_title") or "")
        self.save_state()
        return result

    # -- 执行 -------------------------------------------------------------
    def _execute(
        self, *, run_id: str, kind: str, payload: dict[str, Any], observer: Any = None
    ) -> RunResult:
        self.settings.ensure_dirs()
        if self.settings.dry_run:
            # dry-run 下写操作不会真的生效，而 agent 会去读回来确认 ——
            # 不提前告知，它就会以为「没生效」而反复重试同一个操作。
            payload.setdefault("notes", []).append(
                "⚠️ 本轮是 dry-run：所有写操作（语雀 + 工作区）都**不会真正生效**。"
                "你写完后读回来看到的是未改动的原状，这是预期的。请按照你判断出的目标形态"
                "把需要的操作做完一次就调 done，**不要因为读回来没变化而重试**。"
            )
            payload["dry_run"] = True
        run_dir = self.settings.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "payload.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        ctx = RunContext(
            settings=self.settings,
            client=self.client,
            run_id=run_id,
            kind=kind,
            run_dir=run_dir,
            toc=list(payload.get("toc") or []),
            docs=self.state.snapshot.docs if self.state.snapshot else {},
            today=_day_of(payload),
        )

        session_path = session_path_for(run_dir)
        with SessionRecorder(session_path) as session:
            result = run_agent(
                llm=self.llm,
                ctx=ctx,
                prompt=self.prompt,
                payload=payload,
                session=session,
                max_steps=self.settings.max_steps,
                max_tool_calls=self.settings.max_tool_calls,
                observer=observer,
            )

        (run_dir / "result.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 留痕 → 语雀《工作日志》
        if self.settings.journal:
            outcome = journal_mod.journal_or_warn(
                self.client, self.settings, session_path, result=result.to_dict()
            )
            result.journal = outcome
            (run_dir / "journal.json").write_text(
                json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # 记下《工作日志》的 doc_id：下次取快照就把它排除在外，
            # 否则「写日志 → 日志变了 → 唤醒 LLM → 又写日志」会自激。
            if outcome.get("doc_id"):
                self.state.journal_doc_id = int(outcome["doc_id"])
                self.save_state()
        return result


# ---------------------------------------------------------------- 归档指令


def build_archive_instruction(
    *,
    run_id_prefix: str,
    settings: Settings,
    snapshot: Snapshot,
    now: datetime,
    guide_body: str = "",
) -> dict[str, Any]:
    """给归档会话的「指令」。

    日历计算由**程序**做（这是纯粹的算术，不该让 LLM 猜）；
    但「现在这棵目录树和目标形态差在哪里、该怎么动」归 LLM。
    """
    today = now.date()
    current, previous = cycle_targets(
        now,
        start_weekday=settings.archive_weekday,
        start_hour=settings.archive_hour,
    )

    root_titles = [n for n in snapshot.toc if n.get("depth") == 1 and n.get("type") == "TITLE"]
    archive_node = next((n for n in root_titles if str(n.get("title") or "") == "归档区"), None)
    return {
        "run_id": run_id_prefix,
        "kind": "archive",
        "at": snapshot.taken_at,
        "repo": {"namespace": settings.repo, "toc_sha": snapshot.toc_sha},
        "today": today.isoformat(),
        "weekday": "周" + "一二三四五六日"[today.weekday()],
        "cycle_title": current.title,
        "cycle_start": current.start.isoformat(),
        "cycle_end": current.end.isoformat(),
        "previous_cycle_title": previous.title,
        "archive_node_uuid": (archive_node or {}).get("uuid", ""),
        "toc": snapshot.toc,
        "root_titles": [str(n.get("title") or "") for n in root_titles],
        "guide_doc_body": guide_body,
        "counts": {"docs_known": len(snapshot.docs)},
        "notes": (
            []
            if archive_node
            else ["根目录还没有「归档区」分组，请先用 toc_create 建一个（如果确实需要）。"]
        ),
    }


def _guide_body() -> str:
    path = Path(__file__).parent / "kb" / "guide.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# ---------------------------------------------------------------- 便捷函数


def current_cycle_title(today: datetime | None = None) -> str:
    """当前周期的目录名（仅用于日志/自检展示）。"""
    anchor = today or clock.now()
    current, _ = cycle_targets(anchor)
    return current.title
