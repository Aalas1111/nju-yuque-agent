"""留痕层：session 渲染 + 写回语雀《工作日志》。"""

from __future__ import annotations

import json

import pytest

from tests.fakes import FakeYuque
from yuque_agent.config import Settings
from yuque_agent.journal import HEADER, append_to_journal, render_session, split_journal
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


def test_append_creates_doc_and_hangs_it_in_the_toc(settings: Settings) -> None:
    """新建《工作日志》时要**挂进目录**（社员在侧边栏看得见），并带上表头。

    反向锁：旧版故意不挂目录，需求方在语雀侧边栏看不到它——
    见 `docs/design.md` D3 与 `docs/test-report.md` 的 Bug 5。
    """
    client = FakeYuque()
    client.docs = lambda: []  # type: ignore[method-assign]
    created = client.create_doc
    # 真语雀建完文档会回一个 id；假件默认不回，于是「挂进目录」那一步在测试里从不发生
    client.create_doc = lambda **kw: {**created(**kw), "id": 777}  # type: ignore[method-assign]
    result = append_to_journal(client, settings, "## 一节\n\n内容")  # type: ignore[arg-type]
    assert result["created"] is True
    assert [op for op, _ in client.calls] == ["create_doc", "toc_add"]
    _, kwargs = client.calls[0]
    assert HEADER.splitlines()[0] in kwargs["body"] and "## 一节" in kwargs["body"]
    assert client.calls[1][1]["doc_ids"] == [777]


def _log_meta():
    from yuque_agent.yuque import DocMeta

    return DocMeta(
        doc_id=9,
        slug="log",
        title="工作日志",
        updated_at="t",
        created_at="c",
        author="",
        author_login="",
        word_count=0,
    )


def _existing_log(body: str):
    """造一个「读回来」的日志文档。``body`` 就是语雀看回去的那份正文。"""
    from yuque_agent.yuque import DocDetail

    meta = _log_meta()
    detail = DocDetail(**meta.__dict__, body=body)
    client = FakeYuque(doc_metas=[meta])
    client.docs = lambda: [meta]  # type: ignore[method-assign]
    client.doc = lambda ref: detail  # type: ignore[method-assign]
    return client


def test_append_inserts_above_older_sections(settings: Settings) -> None:
    client = _existing_log(f"{HEADER}\n\n## 2026-09-20T10:00:00+08:00 · 旧的\n\n老内容\n")

    result = append_to_journal(  # type: ignore[arg-type]
        client, settings, "## 2026-09-21T10:00:00+08:00 · 新的\n\n新内容"
    )

    assert result["created"] is False
    op, kwargs = client.calls[0]
    assert op == "update_doc"
    body = kwargs["body"]
    assert body.index("## 2026-09-21") < body.index("## 2026-09-20"), "最新的必须插在上面"
    assert body.count("# 工作日志") == 1, "表头不能重复"


def test_append_survives_yuque_stripping_html(settings: Settings) -> None:
    """**回归：语雀会把 HTML 剥掉，所以不能靠 HTML 注释定位插入点。**

    实测在真机上踩到的：写进去的 ``<!-- NEWEST -->`` 读回来就没了（连同 ``<details>``
    一起被规范化掉），于是每次都走进「没有标记」的分支——把 HEADER 重贴一遍、
    再把旧正文甩到文末。结果是**每跑一轮文末就多堆一份表头**，日志会无限膨胀。

    当时的测试之所以是绿的，是因为 fake 太善良：它把带标记的原始 HEADER 原样喂回去，
    而真实的语雀不会。下面这个 fake 就照着语雀的真实行为造：
    引用的换行被重排、HTML 注释被删。
    """

    def yuque_normalize(text: str) -> str:
        """模仿语雀读回 markdown 时的规范化：删 HTML 注释、重排引用。"""
        out = []
        for line in text.splitlines():
            if line.strip().startswith("<!--"):
                continue
            out.append(line)
        return "\n".join(out)

    normalized_header = yuque_normalize(HEADER)
    assert "<!--" not in normalized_header, "前提：语雀会把注释删掉"

    client = _existing_log(f"{normalized_header}\n\n## 2026-09-20T10:00:00+08:00 · 旧的\n\n老\n")
    append_to_journal(client, settings, "## 2026-09-21T10:00:00+08:00 · 新的\n\n新")  # type: ignore[arg-type]
    body = client.calls[0][1]["body"]

    assert body.count("# 工作日志") == 1, f"表头被重复了：\n{body}"
    assert body.index("## 2026-09-21") < body.index("## 2026-09-20")


def test_repeated_appends_do_not_accumulate_headers(settings: Settings) -> None:
    """连写 10 次，表头也必须只有 1 份——这是那个真机 bug 的直接断言。"""

    def yuque_normalize(text: str) -> str:
        return "\n".join(line for line in text.splitlines() if not line.strip().startswith("<!--"))

    current = yuque_normalize(HEADER)
    for i in range(10):
        client = _existing_log(current)
        append_to_journal(  # type: ignore[arg-type]
            client, settings, f"## 2026-09-2{i}T10:00:00+08:00 · 第{i}次\n\n内容{i}"
        )
        current = yuque_normalize(client.calls[0][1]["body"])

    assert current.count("# 工作日志") == 1, (
        f"写了 10 次，表头出现 {current.count('# 工作日志')} 次"
    )
    assert current.index("第9次") < current.index("第0次"), "最新的应当在最上面"


def test_append_uses_slug_not_doc_id_for_update(settings: Settings) -> None:
    """回归：update_doc 必须用 slug 而不是 doc_id（语雀 API 只接受 slug）。"""
    client = _existing_log(f"{HEADER}\n\n旧内容\n")
    result = append_to_journal(client, settings, "新内容")  # type: ignore[arg-type]

    assert result["created"] is False
    op, kwargs = client.calls[0]
    assert op == "update_doc"
    # 关键断言：第一个参数必须是 slug（"log"），不是 doc_id（9）
    assert kwargs.get("doc_id") == "log" or (
        len(client.calls[0]) > 1 and client.calls[0][1].get("doc_id") == "log"
    ), f"update_doc 应该用 slug 'log'，但实际参数是 {kwargs}"


def test_append_url_uses_settings_host(settings: Settings) -> None:
    """回归：URL 必须用 settings.host，不能硬编码 nova.yuque.com。"""
    settings.host = "https://custom.yuque.com"
    client = _existing_log(f"{HEADER}\n\n旧内容\n")
    result = append_to_journal(client, settings, "新内容")  # type: ignore[arg-type]

    assert result["url"].startswith("https://custom.yuque.com/")
    assert "nova.yuque.com" not in result["url"]


def test_split_journal_handles_empty_and_header_only() -> None:
    assert split_journal("") == ("", "")
    assert split_journal(HEADER) == (HEADER, "")
    head, tail = split_journal(HEADER + "\n## 2026-09-21T00:00:00+08:00 · x\n\n正文")
    assert head.strip().startswith("# 工作日志")
    assert tail.startswith("## 2026-09-21")


def test_append_is_skipped_in_dry_run(settings: Settings) -> None:
    settings.dry_run = True
    client = FakeYuque()
    result = append_to_journal(client, settings, "## 一节")  # type: ignore[arg-type]
    assert result["dry_run"] is True
    assert client.calls == []
