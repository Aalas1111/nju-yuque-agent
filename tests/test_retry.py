"""失败重试的记账：轮询回滚基线、归档冷却重试。

**为什么要这一组**（2026-09-27 定的）：
变更是在跑 LLM **之前**就写进基线的（防进程被重启后重复处理），所以一轮失败如果不退，
这批变更就永远不会再出现在 diff 里——社员什么通知都收不到，而《指导文档》向他承诺过会收到。
另一方面，key 过期这类故障每轮都会失败，所以必须有上限（轮询 3 次、归档每周期 3 次）：
上限之外按老语义「认赔」，但结果里要留一笔（`/log` 看得见）。
"""

from __future__ import annotations

import json
from datetime import timedelta

from tests.fakes import FakeLLM, FakeYuque, call, make_meta, make_toc
from tests.test_control import _valid_raw
from tests.test_control import make_settings as _control_settings
from tests.test_control import write_request as _write_request
from tests.test_debounce import dt
from yuque_agent import control
from yuque_agent.config import Settings
from yuque_agent.llm import LLMError
from yuque_agent.runner import ARCHIVE_MAX_FAILURES, MAX_RUN_RETRIES, Runner, State
from yuque_agent.session import read_events
from yuque_agent.watcher import Watcher

T0 = dt("2026-09-20T10:00:00")  # 周日
SAT = dt("2026-09-19T00:01:00")  # 周六（周期翻转日）


class FailingLLM:
    """一调就抛的 LLM（模拟网络断了 / key 过期）。"""

    model = "fake-failing"
    send_reasoning_back = True

    def __init__(self, message: str = "网络错误（ConnectError）：模拟断了") -> None:
        self.message = message
        self.calls = 0

    def chat(self, messages, *, tools=None):  # noqa: ANN001
        self.calls += 1
        raise LLMError(self.message)

    def close(self) -> None:
        pass


def make_runner(tmp_path, *, llm=None, quiet_seconds: int = 0) -> tuple[Runner, FakeYuque]:
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws", quiet_seconds=quiet_seconds)
    settings.ensure_dirs()
    client = FakeYuque()
    runner = Runner(settings=settings, client=client, llm=llm or FailingLLM())  # type: ignore[arg-type]
    return runner, client


def set_docs(client: FakeYuque, *docs: tuple[int, str], updated: str = "t1") -> None:
    client.doc_metas = [make_meta(doc_id, title, updated_at=updated) for doc_id, title in docs]
    client.toc_nodes = make_toc(
        ("0919-0925", "TITLE", 0, ""),
        *[(title, "DOC", doc_id, "0919-0925") for doc_id, title in docs],
    )
    for doc_id, title in docs:
        client.bodies[doc_id] = f"申请人：{title}"


def done_response():
    return call("done", verdict="nothing_to_do", summary="无事")


# ---------------------------------------------------------------- 轮询：失败回滚


def test_failed_run_rolls_back_the_baseline(tmp_path) -> None:
    """一轮跑挂了要把基线退回——否则这批变更永远不会再被检测到。"""
    runner, client = make_runner(tmp_path)
    set_docs(client)
    assert runner.poll_once(now=T0) is None  # 基线（0 token）

    set_docs(client, (1, "新生见面会"))
    result = runner.poll_once(now=T0 + timedelta(seconds=1))

    assert result is not None
    assert result.error, "模拟的 LLM 故障应当被记成 error"
    assert result.retry["rolled_back"] is True
    assert result.retry["attempt"] == 1
    assert runner.state.retry_failures == 1
    assert runner.state.snapshot is not None and 1 not in runner.state.snapshot.docs, (
        "基线必须退回：这篇文档要能在下一轮重新变成 added"
    )


def test_retry_succeeds_once_the_llm_recovers(tmp_path) -> None:
    """退回之后，LLM 一恢复，同一批变更就会被重新检测并处理（不用等社员再改一次）。"""
    runner, client = make_runner(tmp_path)
    set_docs(client)
    runner.poll_once(now=T0)

    set_docs(client, (1, "新生见面会"))
    assert runner.poll_once(now=T0 + timedelta(seconds=1)).retry["rolled_back"] is True

    runner.llm = FakeLLM(script=[done_response()])  # type: ignore[assignment]
    result = runner.poll_once(now=T0 + timedelta(seconds=2))

    assert result is not None and result.error == ""
    assert result.verdict == "nothing_to_do"
    assert runner.state.retry_failures == 0, "成功一轮要把连续失败计数清零"
    assert runner.state.snapshot is not None and 1 in runner.state.snapshot.docs


def test_polling_gives_up_after_the_retry_cap(tmp_path) -> None:
    """连续失败到上限就放弃这一批——不然 key 过期这类故障会一直白跑。"""
    runner, client = make_runner(tmp_path)
    set_docs(client)
    runner.poll_once(now=T0)
    set_docs(client, (1, "新生见面会"))

    seconds = 1
    for _ in range(MAX_RUN_RETRIES):
        result = runner.poll_once(now=T0 + timedelta(seconds=seconds))
        assert result.retry["rolled_back"] is True
        seconds += 1

    result = runner.poll_once(now=T0 + timedelta(seconds=seconds))
    assert result.retry.get("gave_up") is True
    assert runner.state.retry_failures == 0, "放弃之后重新计数（下一批变更有自己的 3 次机会）"
    assert runner.state.snapshot is not None and 1 in runner.state.snapshot.docs, (
        "放弃 = 按老语义认赔，基线不再退回"
    )


def test_stopped_without_done_is_retried_but_half_done_is_not(tmp_path) -> None:
    """「模型自己停了、什么都没判定」要重试；「做到一半被截住」不重试（会重复发通知）。"""
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws", quiet_seconds=0, max_steps=2)
    settings.ensure_dirs()
    client = FakeYuque()
    llm = FakeLLM(script=[])  # 脚本空 = 返回「(script exhausted)」，不带任何工具调用
    runner = Runner(settings=settings, client=client, llm=llm)  # type: ignore[arg-type]
    set_docs(client)
    runner.poll_once(now=T0)
    set_docs(client, (1, "新生见面会"))

    stopped = runner.poll_once(now=T0 + timedelta(seconds=1))
    assert stopped.stop_reason == "llm_stopped_without_done"
    assert stopped.retry["rolled_back"] is True

    llm.script = [call("kb_tree"), call("kb_tree"), call("kb_tree"), call("kb_tree")]
    halfway = runner.poll_once(now=T0 + timedelta(seconds=2))
    assert halfway.stop_reason == "max_steps"
    assert halfway.retry == {}, "max_steps 不重试（已做的部分要留着，重跑会重复发通知）"


# ---------------------------------------------------------------- 归档：冷却重试


def archive_watcher(tmp_path) -> tuple[Watcher, Runner]:
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws", quiet_seconds=0)
    settings.ensure_dirs()
    runner = Runner(settings=settings, client=FakeYuque(), llm=FailingLLM())  # type: ignore[arg-type]
    return Watcher(runner=runner, settings=settings, log=lambda _t: None), runner


def test_archive_failure_does_not_advance_the_watermark(tmp_path) -> None:
    watcher, runner = archive_watcher(tmp_path)
    assert watcher.archive_due(SAT) is True

    result = runner.archive_once(now=SAT)

    assert result.error
    assert result.retry["attempt"] == 1
    assert runner.state.last_archive_title == "", "水位线必须留着：这个周期还没做成"
    assert runner.state.archive_failures == 1


def test_archive_retries_only_after_the_cooldown(tmp_path) -> None:
    watcher, runner = archive_watcher(tmp_path)
    runner.archive_once(now=SAT)

    assert watcher.archive_due(SAT + timedelta(minutes=1)) is False, "冷却期内不许再烧 2 万 token"
    cooling = watcher.archive_retry_at(SAT + timedelta(minutes=1))
    assert cooling is not None and cooling > SAT + timedelta(minutes=1)
    assert watcher.archive_due(SAT + timedelta(minutes=31)) is True, "冷却结束就该重试"


def test_archive_gives_up_after_the_cap_and_moves_on(tmp_path) -> None:
    watcher, runner = archive_watcher(tmp_path)
    moment = SAT
    for _ in range(ARCHIVE_MAX_FAILURES - 1):
        runner.archive_once(now=moment)
        assert runner.state.last_archive_title == ""
        moment += timedelta(minutes=31)

    result = runner.archive_once(now=moment)

    assert result.retry.get("gave_up") is True
    assert runner.state.last_archive_title == "0919-0925", "放弃 = 推进水位线，等下周六的常规窗口"
    assert runner.state.archive_failures == 0
    assert watcher.archive_due(moment + timedelta(days=1)) is False


def test_archive_success_resets_the_failure_counter(tmp_path) -> None:
    watcher, runner = archive_watcher(tmp_path)
    runner.archive_once(now=SAT)
    assert runner.state.archive_failures == 1

    runner.llm = FakeLLM(script=[done_response()])  # type: ignore[assignment]
    result = runner.archive_once(now=SAT + timedelta(minutes=31))

    assert result.error == ""
    assert runner.state.archive_failures == 0
    assert runner.state.last_archive_title == "0919-0925"
    assert watcher.archive_due(SAT + timedelta(minutes=32)) is False


def test_archive_tools_see_the_fresh_snapshot(tmp_path) -> None:
    """归档的工具上下文要用**这一轮刚拿的快照**，而不是上一次轮询的旧基线。

    冷启动时 state.snapshot 是空的——旧写法会让 doc_read 连「这篇文档在哪个目录」
    都答不上来（`dir` 为空串）。
    """
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws", quiet_seconds=0)
    settings.ensure_dirs()
    client = FakeYuque()
    set_docs(client, (1, "新生见面会"))
    llm = FakeLLM(script=[call("doc_read", doc=1), done_response()])
    runner = Runner(settings=settings, client=client, llm=llm)  # type: ignore[arg-type]

    runner.archive_once(now=SAT)

    tool_messages = [m for m in llm.seen_messages[-1] if m.get("role") == "tool"]
    payload = json.loads(tool_messages[-1]["content"])
    assert payload["ok"] is True
    assert payload["result"]["dir"] == "0919-0925", "归档会话读到的目录必须来自新鲜快照"


# ---------------------------------------------------------------- 控制队列


def test_apply_from_qq_also_notifies_the_admin(tmp_path) -> None:
    """QQ 自助申请不经过 LLM，所以「清单已更新」要在控制队列这条路上补发。"""
    settings = _control_settings(tmp_path)
    runner, _ = make_runner(tmp_path, llm=FakeLLM())

    _write_request(
        settings,
        {
            "kind": "apply",
            "requested_by": "u-7",
            "target": {"scope": "c2c", "target_id": "u-7"},
            "raw": _valid_raw(),
        },
    )
    control.process_pending(settings, runner=runner, log=lambda _t: None)

    kinds = sorted(
        json.loads(path.read_text(encoding="utf-8"))["kind"]
        for path in (settings.notify_dir / "pending").glob("*.json")
    )
    assert "accepted" in kinds, "申请人该收到受理通知"
    assert "plan_updated" in kinds, "cac 也该被提醒去下载新清单"


def test_failed_run_receipt_is_not_silently_ok(tmp_path) -> None:
    """跑挂了要在回执里如实说 ok=False——QQ 桥把它当失败显示，别让它看起来像成功。"""
    from tests.fakes import FakeRunner, FakeRunResult

    settings = _control_settings(tmp_path)
    runner = FakeRunner(poll_results=[FakeRunResult(error="LLMError: 401 key 无效")])
    _write_request(settings, {"kind": "once", "requested_by": "u-1"})

    results = control.process_pending(settings, runner=runner, log=lambda _t: None)

    assert results[0]["ok"] is False
    assert "401" in results[0]["error"]
    receipt = json.loads(
        sorted(settings.control_done_dir.glob("*.json"))[-1].read_text(encoding="utf-8")
    )
    assert receipt["ok"] is False and "401" in receipt["error"]


def test_retry_state_survives_a_restart(tmp_path) -> None:
    """计数要落盘：重启后不该把「已经失败过两次」忘了（否则可能无限重试）。"""
    runner, client = make_runner(tmp_path)
    set_docs(client)
    runner.poll_once(now=T0)
    set_docs(client, (1, "新生见面会"))
    runner.poll_once(now=T0 + timedelta(seconds=1))
    runner.poll_once(now=T0 + timedelta(seconds=2))

    revived = Runner(settings=runner.settings, client=client, llm=FailingLLM())  # type: ignore[arg-type]
    assert revived.state.retry_failures == 2
    assert State.from_json(revived.state.to_json()).retry_failures == 2


def test_load_state_tolerates_garbage_and_old_keys(tmp_path) -> None:
    """state.json 坏了要当「空状态」继续跑，老键（journal_doc_id）要认。"""
    from yuque_agent.runner import load_state

    path = tmp_path / "state.json"

    assert load_state(path).retry_failures == 0, "文件不存在"
    path.write_text("{半截 JSON", encoding="utf-8")
    assert load_state(path).snapshot is None, "坏 JSON → 空状态，而不是抛异常"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_state(path).rounds == 0, "不是对象 → 空状态"
    path.write_text(json.dumps({"journal_doc_id": 42}), encoding="utf-8")
    assert load_state(path).notice_doc_id == 42, "《工作日志》时代的旧键要兼容"


def test_runner_records_the_session_for_a_failed_run(tmp_path) -> None:
    """失败的一轮也要留痕（现场最值钱）：session 里有 error 事件、result.json 有 retry 记账。"""
    runner, client = make_runner(tmp_path)
    set_docs(client)
    runner.poll_once(now=T0)
    set_docs(client, (1, "新生见面会"))

    result = runner.poll_once(now=T0 + timedelta(seconds=1))

    run_dir = runner.settings.runs_dir / result.run_id
    events = list(read_events(run_dir / "session.jsonl"))
    assert any(e.get("t") == "error" for e in events)
    stored = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert stored["error"]
    assert stored["retry"]["rolled_back"] is True
