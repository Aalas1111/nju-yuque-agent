"""渲染层：把 ``session.jsonl`` 渲染成人话（``yqa render`` 用）。"""

from __future__ import annotations

import json

from yuque_agent.render import render_session
from yuque_agent.session import SessionRecorder


def write_session(path, events: list[tuple[str, dict]]) -> None:
    with SessionRecorder(path) as rec:
        for kind, fields in events:
            rec.event(kind, **fields)


def sample_session(path) -> None:
    write_session(
        path,
        [
            ("run_start", {"run_id": "r1", "kind": "polling", "tools": ["kb_tree"], "model": "m"}),
            ("system", {"content": "你是负责人"}),
            (
                "user",
                {
                    "content": json.dumps(
                        {"counts": {"added": 1, "updated": 0, "removed": 0}, "toc_changed": False}
                    )
                },
            ),
            (
                "assistant",
                {
                    "step": 1,
                    "content": "",
                    "reasoning": "先看一下文档",
                    "tool_calls": [{"id": "c1", "name": "doc_read"}],
                    "usage": {"in": 10, "out": 5},
                },
            ),
            (
                "tool",
                {
                    "step": 1,
                    "call_id": "c1",
                    "name": "doc_read",
                    "args": {"doc": 1},
                    "ok": True,
                    "result": {"ok": True, "result": {"body": "申请人：张三"}},
                    "ms": 12,
                },
            ),
            (
                "assistant",
                {
                    "step": 2,
                    "content": "",
                    "reasoning": "",
                    "tool_calls": [{"id": "c2", "name": "done"}],
                    "usage": {"in": 20, "out": 8},
                },
            ),
            (
                "run_end",
                {
                    "at": "2026-09-21T00:05:12+08:00",
                    "steps": 2,
                    "tool_calls": 2,
                    "verdict": "accepted",
                    "summary": "受理了一篇",
                    "stop_reason": "done",
                    "error": "",
                    "usage": {"in": 30, "out": 13},
                },
            ),
        ],
    )


def test_render_includes_verdict_and_summary(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    sample_session(path)
    text = render_session(path)
    assert "polling" in text
    assert "`accepted`" in text
    assert "受理了一篇" in text


def test_render_preserves_reasoning_and_tool_io(tmp_path) -> None:
    """渲染只排版、不美化——思考与工具原始进出必须能看见。"""
    path = tmp_path / "session.jsonl"
    sample_session(path)
    text = render_session(path)
    assert "先看一下文档" in text
    assert "doc_read" in text
    assert "申请人：张三" in text


def test_render_reports_counts_from_report(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    sample_session(path)
    assert "新增 1" in render_session(path)


def test_render_survives_empty_session(tmp_path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert render_session(path)


def test_render_survives_corrupt_lines(tmp_path) -> None:
    path = tmp_path / "broken.jsonl"
    path.write_text(
        '{"t":"run_start","kind":"polling"}\n不是 JSON\n{"t":"run_end","at":"x"}\n',
        encoding="utf-8",
    )
    text = render_session(path)
    assert "polling" in text
