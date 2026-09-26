"""《Agent 通知》文档：本周期通知的对外窗口。

它是**程序维护**的：内容 = 本周期内 agent 发出去的处理通知（重建，不是追加），
所以「周期翻转时清空」不需要额外动作——重建出来自然就是空的。
给 cac（指导老师）的 `plan_updated`（申请清单已更新）不算在内。
"""

from __future__ import annotations

import json

import pytest

from tests.fakes import FakeYuque
from yuque_agent import noticedoc
from yuque_agent.config import Settings


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings(repo="g/kb", workspace=tmp_path / "ws")
    s.ensure_dirs()
    return s


def emit(
    settings: Settings,
    *,
    seq: int,
    kind: str,
    created_at: str,
    title: str = "新生见面会",
    member: str = "",
    message: str = "已受理。",
    folder: str = "pending",
) -> dict:
    """往工作区里放一条通知（真的落文件，走读路径而不是造内存对象）。"""
    record = {
        "schema_version": "1.0",
        "seq": seq,
        "notice_id": f"n{seq}",
        "created_at": created_at,
        "kind": kind,
        "repo": "g/kb",
        "doc": {"doc_id": 1, "title": title} if title else {},
        "member": {"name": member},
        "summary": f"第 {seq} 条",
        "message": message,
        "reasons": [],
        "warnings": [],
        "extra": {},
    }
    path = settings.notify_dir / folder / f"{seq:06d}-{kind}-n{seq}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return record


def doc_meta(doc_id: int = 9, title: str = ""):
    from yuque_agent.yuque import DocMeta

    return DocMeta(
        doc_id=doc_id,
        slug="notice-slug",
        title=title or "Agent 通知",
        updated_at="t",
        created_at="c",
        author="",
        author_login="",
        word_count=0,
    )


def client_with_existing(body: str, meta=None) -> FakeYuque:
    """一个「知识库里已经有这篇文档」的假件：docs() 能列到、doc() 能读回正文。"""
    from yuque_agent.yuque import DocDetail

    meta = meta or doc_meta()
    detail = DocDetail(**meta.__dict__, body=body)
    client = FakeYuque(doc_metas=[meta])
    client.docs = lambda: [meta]  # type: ignore[method-assign]
    client.doc = lambda ref: detail  # type: ignore[method-assign]
    return client


# ---------------------------------------------------------------- 选哪些通知


def test_only_this_cycle_and_never_the_plan_notice(settings: Settings) -> None:
    """本周期之外的、以及给 cac 的「清单已更新」都不进这个文档。"""
    emit(settings, seq=1, kind="accepted", created_at="2026-09-26T20:20:00+08:00")
    emit(settings, seq=2, kind="rejected", created_at="2026-09-25T10:00:00+08:00")  # 上一周期
    emit(settings, seq=3, kind="plan_updated", created_at="2026-09-26T20:30:00+08:00")
    emit(settings, seq=4, kind="accepted", created_at="2026-09-26T21:00:00+08:00", folder="done")

    picked = noticedoc.notices_for_cycle(settings, "0926-1002")
    assert [n["seq"] for n in picked] == [4, 1], "只留本周期的，且最新在前"


def test_counts_notices_across_all_four_folders(settings: Settings) -> None:
    """投递方会把文件搬去 done/unrouted/failed——搬走不等于「没发生过」。"""
    for seq, folder in ((1, "pending"), (2, "done"), (3, "unrouted"), (4, "failed")):
        emit(
            settings,
            seq=seq,
            kind="accepted",
            created_at="2026-09-26T10:00:00+08:00",
            folder=folder,
        )
    assert len(noticedoc.notices_for_cycle(settings, "0926-1002")) == 4


# ---------------------------------------------------------------- 渲染


def test_render_is_newest_first_and_says_people_words(settings: Settings) -> None:
    emit(
        settings,
        seq=1,
        kind="accepted",
        created_at="2026-09-26T10:00:00+08:00",
        title="新生见面会",
        message="「新生见面会」已受理：9 月 27 日 16:10-18:00，仙林校区。",
    )
    emit(
        settings,
        seq=2,
        kind="rejected",
        created_at="2026-09-26T20:20:00+08:00",
        title="桌游之夜",
        message="时间填反了，改完保存就行。",
    )
    body = noticedoc.render(settings, "0926-1002")

    assert body.index("20:20") < body.index("10:00"), "最新的必须在最上面"
    assert "已受理" in body and "已退回" in body
    assert "「新生见面会」已受理" in body and "改完保存就行" in body
    # 说人话：不带工具调用 / 思考过程 / session 这些调试词汇
    for noise in ("tool_calls", "kb_tree", "session.jsonl", "reasoning"):
        assert noise not in body


def test_render_of_an_empty_cycle_says_so(settings: Settings) -> None:
    body = noticedoc.render(settings, "0926-1002")
    assert "本周期还没有通知" in body
    assert body.startswith("> 本周期内"), "正文不该再有 `# Agent 通知`（语雀标题已经有一个了）"


def test_render_matches_what_yuque_gives_back(settings: Settings) -> None:
    """渲染结果必须与「语雀读回来的形式」逐字一致——否则每轮刷新都会白写一次。

    2026-09-26 实测踩到：`## 时间 · 类型` 与 `**《标题》**` 之间多一个空行，
    语雀读回时那个空行被规范化掉，于是刷新永远判定「内容变了」（`unchanged: False`）。
    """
    emit(settings, seq=1, kind="accepted", created_at="2026-09-26T20:20:00+08:00")
    lines = noticedoc.render(settings, "0926-1002").splitlines()
    head = next(i for i, line in enumerate(lines) if line.startswith("## "))
    assert lines[head + 1].startswith("**《"), f"标题下面不该有空行：{lines[head : head + 3]!r}"
    assert lines[-1] == "---", "条目以 --- 收尾"


# ---------------------------------------------------------------- 写文档


def test_refresh_creates_the_doc_and_hangs_it_in_the_tree(settings: Settings) -> None:
    """文档不存在就建一个，并**挂进目录**（否则社员在侧边栏看不到）。"""
    emit(settings, seq=1, kind="accepted", created_at="2026-09-26T20:20:00+08:00")
    client = FakeYuque()
    client.docs = lambda: []  # type: ignore[method-assign]
    created = client.create_doc
    client.create_doc = lambda **kw: {**created(**kw), "id": 777}  # type: ignore[method-assign]

    outcome = noticedoc.refresh(settings, client, cycle="0926-1002")  # type: ignore[arg-type]

    assert outcome["ok"] and outcome["created"] is True
    assert [op for op, _ in client.calls] == ["create_doc", "toc_add"]
    assert client.calls[0][1]["title"] == settings.notice_title
    assert client.calls[1][1]["doc_ids"] == [777]
    assert "已受理" in client.calls[0][1]["body"]


def test_refresh_updates_with_slug_and_only_when_content_changed(settings: Settings) -> None:
    emit(settings, seq=1, kind="accepted", created_at="2026-09-26T20:20:00+08:00")
    stale = "旧的正文"
    client = client_with_existing(stale)

    outcome = noticedoc.refresh(settings, client, cycle="0926-1002")  # type: ignore[arg-type]
    assert outcome["ok"] and outcome["unchanged"] is False
    op, kwargs = client.calls[0]
    assert op == "update_doc"
    assert kwargs["doc_id"] == "notice-slug", "语雀只接受 slug，不能拿 doc_id 去更新"

    # 内容已经一致 → 一个字都不写（每轮都重建，省掉没意义的写操作）
    fresh = noticedoc.render(settings, "0926-1002")
    quiet = client_with_existing(fresh)
    again = noticedoc.refresh(settings, quiet, cycle="0926-1002")  # type: ignore[arg-type]
    assert again["unchanged"] is True
    assert quiet.calls == []


def test_refresh_is_idempotent_across_cycles(settings: Settings) -> None:
    """周期一翻，重建出来就是空的——「清空」不需要额外动作。"""
    emit(settings, seq=1, kind="accepted", created_at="2026-09-26T20:20:00+08:00")
    assert noticedoc.notices_for_cycle(settings, "0926-1002"), "前提：本周期有通知"
    assert noticedoc.notices_for_cycle(settings, "1003-1009") == []
    assert "本周期还没有通知" in noticedoc.render(settings, "1003-1009")


def test_refresh_does_not_write_in_dry_run(settings: Settings) -> None:
    settings.dry_run = True
    emit(settings, seq=1, kind="accepted", created_at="2026-09-26T20:20:00+08:00")
    client = FakeYuque()
    outcome = noticedoc.refresh(settings, client, cycle="0926-1002")  # type: ignore[arg-type]
    assert outcome["dry_run"] is True and outcome["count"] == 1
    assert client.calls == []


def test_refresh_returns_error_instead_of_raising(settings: Settings) -> None:
    """写不进去也不能打断这一轮（通知已经在 outbox 里了，那才是事实来源）。"""
    from yuque_agent.yuque import YuqueError

    client = FakeYuque()
    client.docs = lambda: (_ for _ in ()).throw(YuqueError("语雀 500"))  # type: ignore[method-assign]
    outcome = noticedoc.refresh(settings, client, cycle="0926-1002")  # type: ignore[arg-type]
    assert outcome["ok"] is False
    assert "语雀 500" in outcome["error"]
