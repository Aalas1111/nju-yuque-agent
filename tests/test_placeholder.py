"""占位标题过滤：语雀刚建出来的「无标题」空文档不该是变更信号。

这一层比草稿标记**更早一级**：

| 筛子 | 谁生成 | 谁来判 |
|---|---|---|
| 占位标题（「无标题」/「无标题文档」）+ 空正文 | **机器**（语雀新建文档的默认标题） | **程序**（精确字符串匹配） |
| 草稿标记（删了一半/留了一行） | **人** | **LLM**（模糊，需要判断） |

分界线就是：**机器生成的精确哨兵归程序，人写的模糊标记归 LLM。**
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tests.fakes import FakeLLM, FakeYuque, call, make_meta, make_toc
from yuque_agent import clock
from yuque_agent.config import Settings
from yuque_agent.runner import Runner
from yuque_agent.snapshot import Changes, DocSnapshot, drop_placeholders


def dt(text: str) -> datetime:
    """测试里的「现在」**必须用生产时区**（Asia/Shanghai）构造。

    不能写成 ``datetime.fromisoformat(text).astimezone()``——那是系统本地时区，
    跑在 UTC 机器上的 CI 里注入的时刻就和生产路径不是同一个时区了，
    测出来的东西和线上不是一回事。
    """
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=clock.TZ) if parsed.tzinfo is None else parsed.astimezone(clock.TZ)


def done_response():
    return call("done", verdict="nothing_to_do", summary="无事")


class Kb:
    """一个可以随意摆布的知识库。"""

    def __init__(self, client: FakeYuque, dir_title: str = "0919-0925") -> None:
        self.client = client
        self.dir_title = dir_title
        self.docs: list[tuple[int, str, str]] = []  # (doc_id, title, body)

    def set(self, *docs: tuple[int, str, str], updated: str = "t1") -> None:
        self.docs = list(docs)
        self.client.doc_metas = [
            make_meta(doc_id, title, updated_at=updated) for doc_id, title, _ in self.docs
        ]
        self.client.toc_nodes = make_toc(
            (self.dir_title, "TITLE", 0, ""),
            *[(title, "DOC", doc_id, self.dir_title) for doc_id, title, _ in self.docs],
        )
        self.client.bodies = {doc_id: body for doc_id, _, body in self.docs}
        self.client.error_on_doc = set()


def make(tmp_path, *, quiet_seconds: int = 0) -> tuple[Runner, Kb, FakeLLM]:
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws", quiet_seconds=quiet_seconds)
    settings.ensure_dirs()
    client = FakeYuque()
    llm = FakeLLM(script=[done_response()] * 6)
    runner = Runner(settings=settings, client=client, llm=llm)  # type: ignore[arg-type]
    return runner, Kb(client), llm


T0 = dt("2026-09-20T10:00:00")


# ---------------------------------------------------------------- 纯函数


def snap(doc_id: int, title: str) -> DocSnapshot:
    return DocSnapshot(
        doc_id=doc_id,
        slug=f"s{doc_id}",
        title=title,
        updated_at="t",
        created_at="c",
        author="",
        dir="0921-0927",
    )


def test_drop_placeholders_removes_blank_placeholder() -> None:
    changes = Changes(added=[snap(1, "无标题文档"), snap(2, "新生见面会")])
    dropped = drop_placeholders(changes, {1: "", 2: "申请人：张三"}, ("无标题", "无标题文档"))
    assert [d.doc_id for d in changes.added] == [2]
    assert [d.doc_id for d in dropped] == [1]


def test_placeholder_with_content_is_kept() -> None:
    """**关键安全属性**：标题是占位符但正文有内容 → 必须留下（社员手快直接粘了正文）。"""
    changes = Changes(added=[snap(1, "无标题文档")])
    dropped = drop_placeholders(changes, {1: "申请人：张三\n活动日期：…"}, ("无标题", "无标题文档"))
    assert changes.added, "不能把有正文的文档静默丢掉"
    assert dropped == []


def test_whitespace_only_body_counts_as_blank() -> None:
    changes = Changes(added=[snap(1, "无标题")])
    drop_placeholders(changes, {1: "   \n\t "}, ("无标题", "无标题文档"))
    assert changes.added == []


def test_unknown_body_is_not_dropped() -> None:
    """正文读不到 → 宁可白跑一轮，也不漏掉真实申请。"""
    changes = Changes(added=[snap(1, "无标题文档")])
    drop_placeholders(changes, {}, ("无标题", "无标题文档"))
    assert [d.doc_id for d in changes.added] == [1]


def test_removed_placeholder_is_not_a_signal() -> None:
    changes = Changes(removed=[snap(1, "无标题文档"), snap(2, "新生见面会")])
    drop_placeholders(changes, {}, ("无标题", "无标题文档"))
    assert [d.doc_id for d in changes.removed] == [2]


def test_empty_placeholder_list_is_a_noop() -> None:
    changes = Changes(added=[snap(1, "无标题文档")])
    assert drop_placeholders(changes, {1: ""}, ()) == []
    assert len(changes.added) == 1


# ---------------------------------------------------------------- 端到端


def test_brand_new_untitled_doc_never_wakes_the_llm(tmp_path) -> None:
    runner, kb, llm = make(tmp_path)
    kb.set()
    runner.poll_once(now=T0)

    kb.set((1, "无标题文档", ""))
    assert runner.poll_once(now=T0 + timedelta(seconds=30)) is None
    assert runner.poll_once(now=T0 + timedelta(seconds=60)) is None
    assert llm.calls == 0, "刚建出来的空文档不该唤醒 LLM"


def test_api_default_placeholder_is_also_filtered(tmp_path) -> None:
    """官方 API 在 title 为空时自动填的是「无标题」（实测），也要收。"""
    runner, kb, llm = make(tmp_path)
    kb.set()
    runner.poll_once(now=T0)
    kb.set((1, "无标题", ""))
    assert runner.poll_once(now=T0 + timedelta(seconds=30)) is None
    assert llm.calls == 0


def test_typo_title_breaks_the_filter(tmp_path) -> None:
    """只要标题不是精确占位符（哪怕只是错字「无标题文」）→ 立刻变成信号。"""
    runner, kb, llm = make(tmp_path)
    kb.set()
    runner.poll_once(now=T0)
    kb.set((1, "无标题文", ""))
    assert runner.poll_once(now=T0 + timedelta(seconds=30)) is not None
    assert llm.calls == 1


def test_placeholder_with_content_wakes_the_llm(tmp_path) -> None:
    runner, kb, llm = make(tmp_path)
    kb.set()
    runner.poll_once(now=T0)
    kb.set((1, "无标题文档", "申请人：张三\n活动日期：2026-09-23"))
    result = runner.poll_once(now=T0 + timedelta(seconds=30))
    assert result is not None, "有正文就必须交给 LLM 判"
    assert llm.calls == 1


def test_deleting_a_blank_placeholder_is_not_a_signal(tmp_path) -> None:
    runner, kb, llm = make(tmp_path)
    kb.set((1, "无标题文档", ""))
    runner.poll_once(now=T0)
    kb.set()
    assert runner.poll_once(now=T0 + timedelta(seconds=30)) is None
    assert llm.calls == 0


def test_placeholder_filter_runs_before_debounce(tmp_path) -> None:
    """占位空文档**连静默期计时都不该启动**——它根本不是一批「待处理变更」。"""
    runner, kb, llm = make(tmp_path, quiet_seconds=45)
    kb.set()
    runner.poll_once(now=T0)
    kb.set((1, "无标题文档", ""))
    runner.poll_once(now=T0 + timedelta(seconds=20))
    assert runner.state.pending_since == "", "不该进入待处理状态"


def test_placeholder_then_real_title_still_debounces(tmp_path) -> None:
    """「无标题文档 → 无标题文 → 新生见面会」：前面两次都不算信号，最后一次照旧走静默期。"""
    runner, kb, llm = make(tmp_path, quiet_seconds=45)
    kb.set()
    runner.poll_once(now=T0)

    t = T0 + timedelta(seconds=20)
    kb.set((1, "无标题文档", ""))
    assert runner.poll_once(now=t) is None
    kb.set((1, "无标题文", ""))
    assert runner.poll_once(now=t + timedelta(seconds=20)) is not None or llm.calls == 0
    # 静默期内不叫
    assert llm.calls == 0

    kb.set((1, "新生见面会", "申请人：张三"))
    assert runner.poll_once(now=t + timedelta(seconds=40)) is None  # 还在静默期
    assert runner.poll_once(now=t + timedelta(seconds=90)) is not None
    assert llm.calls == 1


def test_dropped_placeholders_are_reported_in_the_report(tmp_path) -> None:
    """被剔除的东西要写在报告里，方便事后算账。"""
    runner, kb, llm = make(tmp_path)
    kb.set()
    runner.poll_once(now=T0)
    kb.set((1, "无标题文档", ""), (2, "新生见面会", "申请人：张三"))
    assert runner.poll_once(now=T0 + timedelta(seconds=30)) is not None
    user = [m for m in llm.seen_messages[-1] if m.get("role") == "user"][-1]
    assert "中间态" in user["content"]
    assert runner.state.placeholder_dropped == 1


def test_fully_settled_placeholder_does_not_wedge_the_loop(tmp_path) -> None:
    """一篇一直没人管的空文档不该让程序反复重算或卡住。"""
    runner, kb, llm = make(tmp_path, quiet_seconds=45)
    kb.set()
    runner.poll_once(now=T0)
    kb.set((1, "无标题文档", ""))
    for offset in range(0, 200, 20):
        runner.poll_once(now=T0 + timedelta(seconds=offset))
    assert llm.calls == 0
    assert runner.state.pending_since == ""
