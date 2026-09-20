"""手搓的 agent loop：往返、留痕、超限保护。"""

from __future__ import annotations

import json

import pytest

from tests.fakes import Ctx, FakeLLM, call
from yuque_agent.agent import run_agent, session_path_for
from yuque_agent.config import Settings
from yuque_agent.llm import LLMResponse, ToolCall, Usage
from yuque_agent.prompts import PromptLoader
from yuque_agent.session import SessionRecorder, read_events


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings(repo="g/kb", workspace=tmp_path / "ws")
    s.ensure_dirs()
    return s


def run(settings: Settings, kind: str, script: list[LLMResponse], **kwargs):
    env = Ctx.build(settings, kind=kind)
    llm = FakeLLM(script=script)
    session_path = session_path_for(env.ctx.run_dir)
    with SessionRecorder(session_path) as session:
        result = run_agent(
            llm=llm,  # type: ignore[arg-type]
            ctx=env.ctx,
            prompt=PromptLoader(),
            payload={
                "run_id": "r1",
                "kind": kind,
                "docs": {"added": [], "updated": [], "removed": []},
            },
            session=session,
            **kwargs,
        )
    return env, llm, result, session_path


def test_normal_round_ends_on_done(settings: Settings) -> None:
    env, _, result, _ = run(
        settings, "polling", [call("done", verdict="nothing_to_do", summary="没事")]
    )
    assert result.stop_reason == "done"
    assert result.verdict == "nothing_to_do"
    assert result.steps == 1
    assert result.error == ""


def test_tool_error_is_fed_back_to_the_model(settings: Settings) -> None:
    """工具失败不能中断整轮——错误要回到模型手里，让它自己想办法。"""
    env, llm, result, _ = run(
        settings,
        "polling",
        [
            call("doc_read", doc=999),  # 假客户端里不存在
            call("done", verdict="gave_up", summary="读不到"),
        ],
    )
    assert result.stop_reason == "done"
    second_call_messages = llm.seen_messages[1]
    tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
    assert tool_messages, "工具结果必须回灌给模型"
    assert json.loads(tool_messages[-1]["content"])["ok"] is False


def test_bad_tool_arguments_do_not_crash_the_loop(settings: Settings) -> None:
    broken = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="doc_read", arguments_raw="{不是合法 JSON")],
        usage=Usage(),
    )
    env, _, result, session_path = run(
        settings, "polling", [broken, call("done", verdict="ok", summary="s")]
    )
    assert result.error == ""
    assert any(e.get("t") == "tool" and e["ok"] is False for e in read_events(session_path))


def test_max_steps_stops_a_runaway_loop(settings: Settings) -> None:
    script = [call("kb_tree") for _ in range(10)]
    env, _, result, _ = run(settings, "polling", script, max_steps=3)
    assert result.stop_reason == "max_steps"
    assert result.steps == 3


def test_max_tool_calls_stops_a_runaway_loop(settings: Settings) -> None:
    script = [call("kb_tree") for _ in range(10)]
    env, _, result, _ = run(settings, "polling", script, max_steps=10, max_tool_calls=2)
    assert result.stop_reason == "max_tool_calls"


def test_model_stopping_without_done_is_recorded(settings: Settings) -> None:
    """模型自己停了却没调 done —— 也算结束，但要留痕，不能当成功。"""
    env, _, result, _ = run(settings, "polling", [LLMResponse(content="我先歇了")])
    assert result.stop_reason == "llm_stopped_without_done"
    assert result.verdict == ""


def test_usage_is_accumulated_across_steps(settings: Settings) -> None:
    env, _, result, _ = run(
        settings,
        "polling",
        [call("kb_tree"), call("done", verdict="ok", summary="s")],
    )
    assert result.usage.prompt_tokens == 20
    assert result.usage.completion_tokens == 10


# ---------------------------------------------------------------- 留痕


def test_session_records_the_whole_story(settings: Settings) -> None:
    env, _, result, session_path = run(
        settings,
        "polling",
        [call("kb_tree"), call("done", verdict="accepted", summary="受理了")],
    )
    events = list(read_events(session_path))
    kinds = [e["t"] for e in events]
    assert kinds[0] == "run_start"
    assert kinds[-1] == "run_end"
    assert "system" in kinds and "user" in kinds
    assert kinds.count("assistant") == 2
    assert kinds.count("tool") == 2

    start = events[0]
    assert start["kind"] == "polling"
    assert "kb_tree" in start["tools"]
    assert "doc_delete" not in start["tools"], "日常轮询的留痕里不该出现写工具"

    end = events[-1]
    assert end["verdict"] == "accepted"
    assert end["summary"] == "受理了"


def test_session_preserves_raw_reasoning(settings: Settings) -> None:
    """思考过程是调试工作流最值钱的证据，必须原样存下来。"""
    thought = "这份文档带了草稿标记，先跳过。"
    response = LLMResponse(
        content="",
        reasoning=thought,
        tool_calls=[
            ToolCall(id="c1", name="done", arguments_raw='{"verdict":"skip","summary":"草稿"}')
        ],
        usage=Usage(),
    )
    env, _, _, session_path = run(settings, "polling", [response])
    assistant = next(e for e in read_events(session_path) if e["t"] == "assistant")
    assert assistant["reasoning"] == thought


def test_session_records_emitted_outputs(settings: Settings) -> None:
    env, _, result, session_path = run(
        settings,
        "polling",
        [
            call("emit_notice", kind="rejected", summary="不行", message="请修改"),
            call("done", verdict="rejected", summary="退回"),
        ],
    )
    end = next(e for e in read_events(session_path) if e["t"] == "run_end")
    assert end["emitted"][0]["type"] == "notice"
    assert result.emitted[0]["type"] == "notice"


def test_archive_session_gets_write_tools_in_its_session_record(settings: Settings) -> None:
    env, _, _, session_path = run(
        settings, "archive", [call("done", verdict="nothing_to_do", summary="")]
    )
    start = next(e for e in read_events(session_path) if e["t"] == "run_start")
    assert "doc_delete" in start["tools"]
    assert "toc_move" in start["tools"]
