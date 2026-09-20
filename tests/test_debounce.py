"""静默期合并（debounce）。

**要锁住的性质**：语雀手工建一篇文档会分几步产生变更
（先出现无标题空文档 → 改标题 → 写正文保存），
不合并的话一篇文档就要唤醒好几次 LLM。

这一组测试直接打在 :meth:`Runner.poll_once` 上，用固定时刻驱动，
断言「到底叫了几次 LLM」。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tests.fakes import FakeLLM, FakeYuque, call, make_meta, make_toc
from yuque_agent.config import Settings
from yuque_agent.llm import LLMResponse, ToolCall, Usage
from yuque_agent.runner import Runner


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone()


def done_response() -> LLMResponse:
    return call("done", verdict="nothing_to_do", summary="无事")


def make_runner(tmp_path, *, quiet_seconds: int = 45) -> tuple[Runner, FakeYuque, FakeLLM]:
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws", quiet_seconds=quiet_seconds)
    settings.ensure_dirs()
    client = FakeYuque()
    llm = FakeLLM(script=[done_response(), done_response(), done_response()])
    runner = Runner(settings=settings, client=client, llm=llm)  # type: ignore[arg-type]
    return runner, client, llm


def set_docs(client: FakeYuque, *docs: tuple[int, str], updated: str = "t1") -> None:
    client.doc_metas = [make_meta(doc_id, title, updated_at=updated) for doc_id, title in docs]
    client.toc_nodes = make_toc(
        ("0919-0925", "TITLE", 0, ""),
        *[(title, "DOC", doc_id, "0919-0925") for doc_id, title in docs],
    )
    for doc_id, title in docs:
        client.bodies[doc_id] = f"申请人：{title}"


# ---------------------------------------------------------------- 合并


def test_burst_of_edits_triggers_exactly_one_llm_call(tmp_path) -> None:
    """核心断言：一串连续变更 → 只叫一次 LLM。"""
    runner, client, llm = make_runner(tmp_path)

    # 基线：知识库空着
    set_docs(client)
    assert runner.poll_once(now=dt("2026-09-20T10:00:00")) is None

    t0 = dt("2026-09-20T10:01:00")
    set_docs(client, (1, "无标题文档"))
    assert runner.poll_once(now=t0) is None, "刚出现变更时不该立刻叫 LLM"

    set_docs(client, (1, "新生见面会"))
    assert runner.poll_once(now=t0 + timedelta(seconds=20)) is None, "还在静默期内"

    set_docs(client, (1, "新生见面会"), (2, "读书会"))
    assert runner.poll_once(now=t0 + timedelta(seconds=30)) is None, "还在静默期内"

    result = runner.poll_once(now=t0 + timedelta(seconds=46))
    assert result is not None, "静默期结束后应当唤醒"
    assert llm.calls == 1, f"整串变更只该叫一次 LLM，实际叫了 {llm.calls} 次"


def test_llm_sees_the_final_state_not_the_intermediate_ones(tmp_path) -> None:
    """LLM 看到的应该是最终状态，而不是「无标题空文档」。"""
    runner, client, llm = make_runner(tmp_path)
    set_docs(client)
    runner.poll_once(now=dt("2026-09-20T10:00:00"))

    t0 = dt("2026-09-20T10:01:00")
    set_docs(client, (1, "无标题文档"), updated="t1")
    runner.poll_once(now=t0)
    set_docs(client, (1, "新生见面会"), updated="t2")
    runner.poll_once(now=t0 + timedelta(seconds=10))
    runner.poll_once(now=t0 + timedelta(seconds=46))

    assert llm.calls == 1
    # 第一轮（index 0）之后的那次调用看到的 user 消息应含最终标题
    user_messages = [m for m in llm.seen_messages[-1] if m.get("role") == "user"]
    assert user_messages and "新生见面会" in user_messages[-1]["content"]
    assert "无标题文档" not in user_messages[-1]["content"]


def test_collapsed_polls_are_reported(tmp_path) -> None:
    """报告里要写明「合并掉了多少次唤醒」，方便事后算账。"""
    runner, client, llm = make_runner(tmp_path)
    set_docs(client)
    runner.poll_once(now=dt("2026-09-20T10:00:00"))

    t0 = dt("2026-09-20T10:01:00")
    set_docs(client, (1, "甲"))
    runner.poll_once(now=t0)
    runner.poll_once(now=t0 + timedelta(seconds=30))
    runner.poll_once(now=t0 + timedelta(seconds=46))

    user_messages = [m for m in llm.seen_messages[-1] if m.get("role") == "user"]
    assert "静默期合并" in user_messages[-1]["content"]


def test_create_then_delete_costs_nothing(tmp_path) -> None:
    """建完又立刻删掉 → 静默期结束时 diff 为空 → 整轮根本不触发。"""
    runner, client, llm = make_runner(tmp_path)
    set_docs(client)
    runner.poll_once(now=dt("2026-09-20T10:00:00"))

    t0 = dt("2026-09-20T10:01:00")
    set_docs(client, (1, "误建的空文档"))
    runner.poll_once(now=t0)

    set_docs(client)  # 删掉了
    assert runner.poll_once(now=t0 + timedelta(seconds=46)) is None
    assert llm.calls == 0, "文档已经不存在了，不该为它叫 LLM"
    assert runner.state.pending_since == ""


def test_edit_back_to_original_costs_nothing(tmp_path) -> None:
    """改了又改回去（正文哈希不变）→ 也不该叫 LLM。"""
    runner, client, llm = make_runner(tmp_path)
    set_docs(client, (1, "甲"))
    runner.poll_once(now=dt("2026-09-20T10:00:00"))

    t0 = dt("2026-09-20T10:01:00")
    set_docs(client, (1, "甲"), updated="t2")  # 时间戳变了、正文没变
    assert runner.poll_once(now=t0 + timedelta(seconds=46)) is None
    assert llm.calls == 0


def test_debounce_can_be_disabled(tmp_path) -> None:
    runner, client, llm = make_runner(tmp_path)
    set_docs(client)
    runner.poll_once(now=dt("2026-09-20T10:00:00"))
    set_docs(client, (1, "甲"))
    result = runner.poll_once(now=dt("2026-09-20T10:01:00"), debounce=False)
    assert result is not None
    assert llm.calls == 1


def test_zero_quiet_seconds_disables_debounce(tmp_path) -> None:
    runner, client, llm = make_runner(tmp_path, quiet_seconds=0)
    set_docs(client)
    runner.poll_once(now=dt("2026-09-20T10:00:00"))
    set_docs(client, (1, "甲"))
    assert runner.poll_once(now=dt("2026-09-20T10:01:00")) is not None
    assert llm.calls == 1


def test_rescan_bypasses_debounce(tmp_path) -> None:
    runner, client, llm = make_runner(tmp_path)
    set_docs(client, (1, "甲"))
    result = runner.poll_once(now=dt("2026-09-20T10:01:00"), rescan=True)
    assert result is not None
    assert llm.calls == 1


def test_force_bypasses_nothing_but_still_needs_quiet(tmp_path) -> None:
    """``--force`` 是「无视 diff」，不是「无视静默期」——手动命令请用 once（它关掉合并）。"""
    runner, client, llm = make_runner(tmp_path)
    set_docs(client)
    runner.poll_once(now=dt("2026-09-20T10:00:00"))
    set_docs(client, (1, "甲"))
    runner.poll_once(now=dt("2026-09-20T10:01:00"), force=True)
    assert llm.calls == 0


# ---------------------------------------------------------------- 状态落盘


def test_pending_state_survives_restart(tmp_path) -> None:
    """进程重启后静默期计时不该重置，否则会被反复打断。"""
    runner, client, llm = make_runner(tmp_path)
    set_docs(client)
    runner.poll_once(now=dt("2026-09-20T10:00:00"))

    t0 = dt("2026-09-20T10:01:00")
    set_docs(client, (1, "甲"))
    runner.poll_once(now=t0)

    revived = Runner(settings=runner.settings, client=client, llm=llm)  # type: ignore[arg-type]
    assert revived.state.pending_since, "重启后应当从 state.json 里读回待处理标记"
    assert revived.poll_once(now=t0 + timedelta(seconds=46)) is not None
    assert llm.calls == 1


def test_own_journal_writes_never_trigger_a_run(tmp_path) -> None:
    """**自激循环回归**：程序自己的《工作日志》变了，不能算「知识库变了」。

    实测踩到过：一篇测试文档触发 13 次 run，其中 8 次是
    「写日志 → 日志变了 → 唤醒 LLM → 又写日志」。
    """
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws", quiet_seconds=0)
    settings.ensure_dirs()
    client = FakeYuque()
    llm = FakeLLM(script=[done_response()] * 4)
    runner = Runner(settings=settings, client=client, llm=llm)  # type: ignore[arg-type]

    # 知识库里有申请文档，另有一篇程序自己写的日志文档
    set_docs(client, (1, "新生见面会"), (99, "工作日志"))
    assert runner.poll_once(now=dt("2026-09-20T10:00:00")) is None  # 基线（静默）
    assert llm.calls == 0

    # 日志文档被程序更新了 → 不该唤醒
    set_docs(client, (1, "新生见面会"), (99, "工作日志"), updated="t1")
    client.doc_metas = [
        make_meta(1, "新生见面会", updated_at="t1"),
        make_meta(99, "工作日志", updated_at="t2"),
    ]
    assert runner.poll_once(now=dt("2026-09-20T10:01:00")) is None
    assert llm.calls == 0, "《工作日志》的变更不该唤醒 LLM（否则会自激）"

    # 但真正来自社员的变更仍要唤醒
    set_docs(client, (1, "新生见面会"), (2, "读书会"), (99, "工作日志"))
    assert runner.poll_once(now=dt("2026-09-20T10:02:00")) is not None
    assert llm.calls == 1


def test_journal_doc_excluded_by_id_even_after_rename(tmp_path) -> None:
    """靠 doc_id 兜底：即使日志被改名，也不会重新变成变更信号。"""
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws", quiet_seconds=0)
    settings.ensure_dirs()
    client = FakeYuque()
    llm = FakeLLM(script=[done_response()] * 3)
    runner = Runner(settings=settings, client=client, llm=llm)  # type: ignore[arg-type]
    runner.state.journal_doc_id = 99

    set_docs(client, (99, "被人改了名的日志"))
    runner.poll_once(now=dt("2026-09-20T10:00:00"))
    client.doc_metas = [make_meta(99, "被人改了名的日志", updated_at="t2")]
    assert runner.poll_once(now=dt("2026-09-20T10:01:00")) is None
    assert llm.calls == 0


def test_baseline_does_not_advance_while_pending(tmp_path) -> None:
    """关键不变量：静默期内基线不推进，所以整串变更始终是「相对于上一轮已处理状态」的。"""
    runner, client, llm = make_runner(tmp_path)
    set_docs(client, (1, "甲"))
    runner.poll_once(now=dt("2026-09-20T10:00:00"))
    baseline_docs = set(runner.state.snapshot.docs) if runner.state.snapshot else set()

    set_docs(client, (1, "甲"), (2, "乙"))
    runner.poll_once(now=dt("2026-09-20T10:01:00"))

    assert set(runner.state.snapshot.docs) == baseline_docs, "静默期内不该推进基线"
    assert runner.state.pending_since


def test_usage_is_not_spent_during_the_quiet_window(tmp_path) -> None:
    """静默期内一轮 LLM 都不该发。"""
    runner, client, llm = make_runner(tmp_path)
    set_docs(client)
    runner.poll_once(now=dt("2026-09-20T10:00:00"))
    set_docs(client, (1, "甲"))
    t0 = dt("2026-09-20T10:01:00")
    for offset in (0, 10, 20, 30, 40):
        runner.poll_once(now=t0 + timedelta(seconds=offset))
    assert llm.calls == 0
    assert runner.state.pending_polls >= 5


def test_llm_error_does_not_wedge_the_loop(tmp_path) -> None:
    """一轮跑挂了也要把基线推进、把待处理标记清掉，否则会卡在原地反复重跑。"""
    runner, client, llm = make_runner(tmp_path)
    set_docs(client)
    runner.poll_once(now=dt("2026-09-20T10:00:00"))
    set_docs(client, (1, "甲"))
    t0 = dt("2026-09-20T10:01:00")
    runner.poll_once(now=t0)

    llm.script = []  # 脚本耗尽 → 返回一句「结束」而不是 done
    result = runner.poll_once(now=t0 + timedelta(seconds=46))
    assert result is not None
    assert runner.state.pending_since == ""
    assert result.stop_reason  # 有明确的停止原因


def test_unused_usage_import_guard() -> None:
    """占位：保证 Usage 被引用（避免 lint 误删导入时静默改变语义）。"""
    assert Usage().to_dict()["total"] == 0
    assert ToolCall(id="x", name="done", arguments_raw="{}").arguments() == {}
