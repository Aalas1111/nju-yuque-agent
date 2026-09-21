"""分段发送 + 保活心跳测试。

要守住的语义只有三条：

1. **一段助手输出 = 一条消息**（不在 token 级发、不把一段拆成好几条刷屏）；
2. **工具调用不发消息**，只用来回答「现在在干什么」；
3. **空闲超时发保活**，且同一条 ``msg_id`` 下 ``msg_seq`` 严格递增。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import Ctx, FakeLLM
from tests.qq_fakes import FakeRunner, FakeRunResult, RecordingSender
from yuque_agent.agent import run_agent
from yuque_agent.config import Settings
from yuque_agent.llm import LLMResponse, Usage
from yuque_agent.qqbot.bridge import NotifyBridge
from yuque_agent.qqbot.client import Target
from yuque_agent.qqbot.config import NotifyTarget, QQBotConfig
from yuque_agent.qqbot.events import InboundMessage
from yuque_agent.qqbot.progress import ProgressOptions, ProgressSender, split_segment
from yuque_agent.qqbot.service import QQBotService, make_progress_observer


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings(repo="g/kb", workspace=tmp_path / "ws")
    s.ensure_dirs()
    return s


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_sender(**kwargs) -> tuple[ProgressSender, RecordingSender]:
    sink = RecordingSender()
    target = Target("c2c", "u-1", "m-1")
    kwargs.setdefault("options", ProgressOptions(idle_seconds=0, min_interval_seconds=0))
    return ProgressSender(sink, target, **kwargs), sink


# ---------------------------------------------------------------- 一段 = 一条


def test_segment_is_sent_as_one_message() -> None:
    sender, sink = make_sender()
    sender.segment("我看了一下这篇申请，要素齐了。")

    assert len(sink.sent) == 1  # 一段就是一条
    target, text = sink.sent[0]
    assert text == "我看了一下这篇申请，要素齐了。"
    assert target.msg_id == "m-1"  # 被动回复
    assert sink.calls_detail[0]["kwargs"]["msg_seq"] == 1


def test_segments_get_increasing_msg_seq() -> None:
    sender, sink = make_sender()
    for text in ("第一段", "第二段", "第三段"):
        sender.segment(text)

    seqs = [call["kwargs"]["msg_seq"] for call in sink.calls_detail]
    assert seqs == [1, 2, 3]
    assert {call["target"].msg_id for call in sink.calls_detail} == {"m-1"}


def test_blank_segment_sends_nothing() -> None:
    sender, sink = make_sender()
    assert sender.segment("") == 0
    assert sender.segment("   \n  ") == 0
    assert sink.sent == []
    assert sender.stats()["sent"] == 0


def test_proactive_target_does_not_send_msg_seq() -> None:
    sink = RecordingSender()
    sender = ProgressSender(
        sink, Target("c2c", "u-1"), options=ProgressOptions(idle_seconds=0, min_interval_seconds=0)
    )
    sender.segment("主动推送没有被动回复窗口")
    assert sink.calls_detail[0]["kwargs"] == {}  # msg_seq 只在有 msg_id 时才带


def test_long_segment_is_split_on_line_boundaries() -> None:
    sender, sink = make_sender(
        options=ProgressOptions(idle_seconds=0, min_interval_seconds=0, max_chars=50)
    )
    text = "\n".join(f"第 {i} 行内容" for i in range(20))  # 每行 7~8 字
    sent = sender.segment(text)

    assert sent > 1
    assert len(sink.sent) == sent
    for _target, chunk in sink.sent:
        assert len(chunk) <= 50
        assert not chunk.startswith("行内容")  # 不切在行中间
    assert "第 0 行内容" in sink.sent[0][1]


def test_split_segment_keeps_single_oversized_line() -> None:
    chunks = split_segment("x" * 25, 10)
    assert chunks == ["x" * 10, "x" * 10, "x" * 5]  # 单行超长只能硬切
    assert split_segment("", 10) == []
    assert split_segment("短", 10) == ["短"]


def test_min_interval_is_respected() -> None:
    slept: list[float] = []
    now = {"t": 0.0}

    def clock() -> float:
        return now["t"]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds  # 睡完时间就前进

    sender = ProgressSender(
        RecordingSender(),
        Target("c2c", "u-1", "m-1"),
        options=ProgressOptions(idle_seconds=0, min_interval_seconds=5),
        clock=clock,
        sleep=sleep,
    )
    sender.segment("一")
    sender.segment("二")  # 距上一条 0s → 必须等待
    assert slept and slept[0] > 0


def test_send_failure_is_counted_and_does_not_raise() -> None:
    sink = RecordingSender(fail_times=1)
    sender = ProgressSender(
        sink,
        Target("c2c", "u-1", "m-1"),
        options=ProgressOptions(idle_seconds=0, min_interval_seconds=0),
    )
    assert sender.segment("这段会失败") == 0
    assert sender.failed == 1 and sender.sent == 0
    assert "模拟发送失败" in sender.stats()["last_error"]
    # 失败之后还能继续发（一次失败不该拖垮整个 run）
    assert sender.segment("这段会成功") == 1
    assert sender.sent == 1 and sink.sent[-1][1] == "这段会成功"


# ---------------------------------------------------------------- 保活


def test_heartbeat_fires_when_idle_with_current_operation() -> None:
    lines: list[str] = []
    sender, sink = make_sender(
        options=ProgressOptions(idle_seconds=0.3, min_interval_seconds=0),
        log=lines.append,
    )
    sender.start()
    try:
        sender.current("调用 doc_read")
        assert wait_for(lambda: any("正在进行" in text for _t, text in sink.sent))
    finally:
        sender.stop()

    target, text = sink.sent[0]
    assert text == "⏳ 正在进行：调用 doc_read"
    assert target.msg_id == "m-1"
    assert sender.stats()["heartbeats"] >= 1
    assert any("保活" in line for line in lines)


def test_heartbeat_does_not_fire_without_current_operation() -> None:
    sender, sink = make_sender(options=ProgressOptions(idle_seconds=0.2, min_interval_seconds=0))
    sender.start()
    try:
        time.sleep(0.5)
        assert sink.sent == []  # 不知道在干什么就别打扰用户
    finally:
        sender.stop()


def test_heartbeat_can_be_disabled() -> None:
    sender, sink = make_sender(options=ProgressOptions(idle_seconds=0, min_interval_seconds=0))
    sender.start()
    try:
        sender.current("调用 doc_read")
        time.sleep(0.3)
        assert sink.sent == []
        assert sender._thread is None  # 不起看门狗线程
    finally:
        sender.stop()


def test_segment_resets_the_idle_timer() -> None:
    sender, sink = make_sender(options=ProgressOptions(idle_seconds=0.4, min_interval_seconds=0))
    sender.start()
    try:
        sender.current("调用 doc_read")
        for _ in range(3):  # 持续有输出时不该插保活
            sender.segment("还在干活")
            time.sleep(0.1)
        assert all("正在进行" not in text for _t, text in sink.sent)
    finally:
        sender.stop()


def test_stop_halts_the_heartbeat() -> None:
    sender, sink = make_sender(options=ProgressOptions(idle_seconds=0.2, min_interval_seconds=0))
    sender.start()
    sender.current("调用 doc_read")
    assert wait_for(lambda: len(sink.sent) >= 1)
    sender.stop()
    count = len(sink.sent)
    time.sleep(0.5)
    assert len(sink.sent) == count  # 停了就不再发


def test_current_alone_never_sends() -> None:
    sender, sink = make_sender()
    sender.current("调用 doc_read")
    sender.current("第 3 步")
    assert sink.sent == []
    assert sender.current_operation == "第 3 步"


# ---------------------------------------------------------------- observer 翻译


def test_observer_sends_assistant_text_and_tracks_tool() -> None:
    sender, sink = make_sender()
    observe = make_progress_observer(sender)

    observe("assistant", step=1, content="我先看看文档。", tool_calls=["doc_read"])
    observe("tool", step=1, name="doc_read")
    observe("assistant", step=2, content="要素齐了，我准备受理。", tool_calls=[])

    assert [text for _t, text in sink.sent] == ["我先看看文档。", "要素齐了，我准备受理。"]
    assert sender.current_operation == "调用 doc_read"


def test_observer_does_not_send_for_tool_only_steps() -> None:
    sender, sink = make_sender()
    observe = make_progress_observer(sender)

    observe("assistant", step=1, content="", tool_calls=["kb_tree", "doc_read"])
    observe("tool", step=1, name="kb_tree")

    assert sink.sent == []  # 工具调用不发消息
    assert sender.current_operation == "调用 kb_tree"


def test_observer_ignores_run_end_and_unknown_kinds() -> None:
    sender, sink = make_sender()
    observe = make_progress_observer(sender)
    observe("run_end", result=object())
    observe("step", step=4)
    assert sink.sent == []
    assert sender.current_operation == "第 4 步"


# ---------------------------------------------------------------- agent 循环


def test_agent_loop_notifies_observer(settings: Settings) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def observe(kind: str, **fields: Any) -> None:
        seen.append((kind, fields))

    llm = FakeLLM(
        script=[
            LLMResponse(
                content="我先读一下文档。",
                tool_calls=[],
                usage=Usage(1, 1, 2),
                # 空 tool_calls → wants_tools False → 循环结束
            )
        ]
    )
    ctx = Ctx.build(settings, client_kwargs={"doc_metas": []}).ctx
    from yuque_agent.prompts import PromptLoader
    from yuque_agent.session import SessionRecorder

    session_path = ctx.run_dir / "session.jsonl"
    with SessionRecorder(session_path) as session:
        run_agent(
            llm=llm,
            ctx=ctx,
            prompt=PromptLoader(),
            payload={"kind": "polling"},
            session=session,
            observer=observe,
        )

    kinds = [kind for kind, _fields in seen]
    assert kinds[:2] == ["assistant", "run_end"]
    assert seen[0][1]["content"] == "我先读一下文档。"


def test_agent_loop_survives_broken_observer(settings: Settings) -> None:
    def boom(_kind: str, **_fields: Any) -> None:
        raise RuntimeError("observer 挂了")

    llm = FakeLLM(script=[LLMResponse(content="hi", usage=Usage(1, 1, 2))])
    ctx = Ctx.build(settings, client_kwargs={"doc_metas": []}).ctx
    from yuque_agent.prompts import PromptLoader
    from yuque_agent.session import SessionRecorder

    with SessionRecorder(ctx.run_dir / "session.jsonl") as session:
        result = run_agent(
            llm=llm,
            ctx=ctx,
            prompt=PromptLoader(),
            payload={"kind": "polling"},
            session=session,
            observer=boom,
        )
    assert result.error == ""  # 观测者坏了不影响这一轮
    from yuque_agent.session import read_events

    kinds = [event["t"] for event in read_events(ctx.run_dir / "session.jsonl")]
    assert "observer_error" in kinds  # 但要留痕


# ---------------------------------------------------------------- 服务接线


def make_service(tmp_path: Path, *, runner: Any, qq_client: Any, **kwargs) -> QQBotService:
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws")
    config = QQBotConfig(
        notify_default=NotifyTarget("group", "g-1"),
        inbound_allow=("u-admin",),
        inbound_admins=("u-admin",),
        inbound_rate_limit=0,  # 这些用例要打到位/忙的分支，不受限流干扰
    )
    bridge = NotifyBridge(notify_dir=settings.notify_dir, sender=qq_client, config=config)
    bridge.ensure_dirs()
    from tests.qq_fakes import FakeWatcher

    return QQBotService(
        settings=settings,
        runner=runner,
        watcher=FakeWatcher(),
        bridge=bridge,
        config=config,
        qq_client=qq_client,
        log=lambda _t: None,
        progress_options=kwargs.pop(
            "progress_options", ProgressOptions(idle_seconds=0, min_interval_seconds=0)
        ),
        **kwargs,
    )


def run_request(service: QQBotService, text: str = "/run") -> None:
    worker = threading.Thread(target=service.work_loop, args=(False,), daemon=True)
    worker.start()
    try:
        result = service.handle_inbound(
            InboundMessage(kind="c2c", sender_id="u-admin", content=text, message_id="m-1")
        )
        assert result["handled"] is True
        assert wait_for(
            lambda: any("跑完了" in t or "没有变化" in t for _x, t in _sender_of(service).sent)
        )
    finally:
        service.stop()
        worker.join(timeout=5)


def _sender_of(service: QQBotService) -> RecordingSender:
    return service.qq_client  # type: ignore[return-value]


def test_qq_run_ack_then_segments_then_summary(tmp_path: Path) -> None:
    sender = RecordingSender()
    runner = FakeRunner(
        poll_results=[FakeRunResult(verdict="accepted", summary="受理了《新生见面会》")],
        events=[
            ("assistant", {"step": 1, "content": "我先看看这篇文档。", "tool_calls": ["doc_read"]}),
            ("tool", {"step": 1, "name": "doc_read"}),
            (
                "assistant",
                {"step": 2, "content": "要素齐了，我受理它。", "tool_calls": ["emit_application"]},
            ),
        ],
    )
    service = make_service(tmp_path, runner=runner, qq_client=sender)
    run_request(service)

    texts = [text for _t, text in sender.sent]
    assert "已排队" in texts[0]  # 回执是第 1 条
    assert "我先看看这篇文档。" in texts[1]
    assert "要素齐了，我受理它。" in texts[2]
    assert "受理了《新生见面会》" in texts[-1]  # 结论是最后一段
    seqs = [c["kwargs"]["msg_seq"] for c in sender.calls_detail]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # 严格递增且不重复
    assert all(c["target"].msg_id == "m-1" for c in sender.calls_detail)  # 延续被动回复


def test_qq_run_heartbeat_shows_current_tool(tmp_path: Path) -> None:
    sender = RecordingSender()

    class SlowRunner(FakeRunner):
        def poll_once(self, **kwargs: Any):  # noqa: ANN003
            self._emit(kwargs.get("observer"))
            time.sleep(0.6)  # 装成一次很慢的 run
            return FakeRunResult(summary="跑完了")

    runner = SlowRunner(events=[("tool", {"step": 1, "name": "doc_read"})])
    service = make_service(
        tmp_path,
        runner=runner,
        qq_client=sender,
        progress_options=ProgressOptions(idle_seconds=0.2, min_interval_seconds=0),
    )
    run_request(service)

    texts = [text for _t, text in sender.sent]
    assert any(text == "⏳ 正在进行：调用 doc_read" for text in texts), texts


def test_progress_can_be_disabled(tmp_path: Path) -> None:
    sender = RecordingSender()
    runner = FakeRunner(
        poll_results=[FakeRunResult(summary="结论")],
        events=[("assistant", {"step": 1, "content": "中间过程不该发出去", "tool_calls": []})],
    )
    service = make_service(tmp_path, runner=runner, qq_client=sender, progress_enabled=False)
    run_request(service)

    texts = [text for _t, text in sender.sent]
    assert "中间过程不该发出去" not in texts
    assert any("结论" in text for text in texts)  # 只保留结论
    assert all(c["kwargs"] == {} for c in sender.calls_detail)  # 走的是老的主动推


def test_busy_agent_does_not_open_a_progress_stream(tmp_path: Path) -> None:
    sender = RecordingSender()

    class BlockingRunner(FakeRunner):
        def poll_once(self, **kwargs: Any):  # noqa: ANN003
            time.sleep(0.4)
            return FakeRunResult(summary="忙完了")

    runner = BlockingRunner()
    service = make_service(tmp_path, runner=runner, qq_client=sender)
    worker = threading.Thread(target=service.work_loop, args=(False,), daemon=True)
    worker.start()
    try:
        assert (
            service.handle_inbound(
                InboundMessage(kind="c2c", sender_id="u-admin", content="/run", message_id="m-1")
            )["handled"]
            is True
        )
        assert wait_for(lambda: service.status()["busy"])
        busy = service.handle_inbound(
            InboundMessage(kind="c2c", sender_id="u-admin", content="/run", message_id="m-2")
        )
        assert "正在忙" in busy["reply"]
        assert busy["data"].get("queued") is False
    finally:
        service.stop()
        worker.join(timeout=5)
    # 只有第一次 /run 开了播报流（回执 + 结论都挂在 m-1 上）；第二次是普通回复
    stream_targets = {c["target"].msg_id for c in sender.calls_detail if c["kwargs"]}
    assert stream_targets == {"m-1"}
    assert any(c["target"].msg_id == "m-2" and not c["kwargs"] for c in sender.calls_detail)


def test_help_command_still_uses_plain_reply(tmp_path: Path) -> None:
    sender = RecordingSender()
    service = make_service(tmp_path, runner=FakeRunner(), qq_client=sender)
    result = service.handle_inbound(
        InboundMessage(kind="c2c", sender_id="u-admin", content="/help", message_id="m-1")
    )
    assert result["handled"] is True
    assert len(sender.sent) == 1
    assert sender.calls_detail[0]["kwargs"] == {}  # 读命令不占 msg_seq
