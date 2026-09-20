"""留痕层：session 渲染 + 写回语雀《工作日志》。"""

from __future__ import annotations

import json

import pytest

from tests.fakes import FakeYuque
from yuque_agent.config import Settings
from yuque_agent.journal import HEADER, MARKER, append_to_journal, render_session
from yuque_agent.session import SessionRecorder


def write_session(path, events: list[tuple[str, dict]]) -> None:
    with SessionRecorder(path) as rec:
        for kind, fields in events:
            rec.event(kind, **fields)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings(repo="g/kb", workspace=tmp_path / "ws", journal=True)
    s.ensure_dirs()
    return s


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


# ---------------------------------------------------------------- 渲染


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


# ---------------------------------------------------------------- 写回语雀


def journal_client(body: str = "") -> FakeYuque:
    client = FakeYuque()
    client.docs = lambda: []  # type: ignore[method-assign]
    return client


def test_append_creates_doc_when_missing(settings: Settings) -> None:
    client = FakeYuque()
    client.docs = lambda: []  # type: ignore[method-assign]
    result = append_to_journal(client, settings, "## 一节\n\n内容")  # type: ignore[arg-type]
    assert result["created"] is True
    op, kwargs = client.calls[0]
    assert op == "create_doc"
    assert MARKER in kwargs["body"] and "## 一节" in kwargs["body"]
    # 关键：不要把这个审计文档挂进知识库目录
    assert all(op != "toc_add" for op, _ in client.calls)


def test_append_inserts_below_marker_newest_first(settings: Settings) -> None:
    from yuque_agent.yuque import DocDetail, DocMeta

    meta = DocMeta(
        doc_id=9,
        slug="log",
        title="工作日志",
        updated_at="t",
        created_at="c",
        author="",
        author_login="",
        word_count=0,
    )
    existing = DocDetail(**meta.__dict__, body=f"{HEADER}\n\n## 旧的\n\n老内容\n")
    client = FakeYuque(doc_metas=[meta])
    client.docs = lambda: [meta]  # type: ignore[method-assign]
    client.doc = lambda ref: existing  # type: ignore[method-assign]

    result = append_to_journal(client, settings, "## 新的\n\n新内容")  # type: ignore[arg-type]
    assert result["created"] is False
    op, kwargs = client.calls[0]
    assert op == "update_doc"
    body = kwargs["body"]
    assert body.index("## 新的") < body.index("## 旧的"), "最新的必须插在上面"


def test_append_is_skipped_in_dry_run(settings: Settings) -> None:
    settings.dry_run = True
    client = FakeYuque()
    result = append_to_journal(client, settings, "## 一节")  # type: ignore[arg-type]
    assert result["dry_run"] is True
    assert client.calls == []
