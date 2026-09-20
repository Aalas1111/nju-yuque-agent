"""快照 / diff / 变更报告。

要锁住的几件事：

1. ``updated_at`` 变了但**正文没变** → 不算变更（语雀挪目录会 bump 时间戳）；
2. 只给客观事实，报告里不能出现任何判定性字段；
3. 删除检测、首次运行、超出预算时的降级行为。
"""

from __future__ import annotations

import json

from tests.fakes import FakeYuque, make_meta, make_toc
from yuque_agent.snapshot import (
    DocSnapshot,
    Snapshot,
    _content_sha,
    build_report,
    compute_changes,
    enrich_and_refine,
    take_snapshot,
)


def snapshot_of(docs: dict[int, DocSnapshot], *, toc_sha: str = "same") -> Snapshot:
    return Snapshot(taken_at="2026-09-20T10:00:00+08:00", docs=docs, toc=[], toc_sha=toc_sha)


def doc(
    doc_id: int, title: str, *, updated_at: str = "t1", dir: str = "0921-0927", sha: str = ""
) -> DocSnapshot:
    return DocSnapshot(
        doc_id=doc_id,
        slug=f"s{doc_id}",
        title=title,
        updated_at=updated_at,
        created_at="c1",
        author="张三",
        dir=dir,
        content_sha256=sha,
    )


# ---------------------------------------------------------------- 基础 diff


def test_first_run_is_flagged_and_yields_no_changes() -> None:
    changes = compute_changes(None, snapshot_of({1: doc(1, "甲")}))
    assert changes.first_run is True
    assert changes.empty is True


def test_added_updated_removed() -> None:
    prev = snapshot_of({1: doc(1, "甲"), 2: doc(2, "乙", updated_at="t1")})
    cur = snapshot_of({2: doc(2, "乙", updated_at="t2"), 3: doc(3, "丙")})
    changes = compute_changes(prev, cur)
    assert [d.doc_id for d in changes.added] == [3]
    assert [n.doc_id for _, n in changes.updated] == [2]
    assert [d.doc_id for d in changes.removed] == [1]
    assert changes.empty is False


def test_unchanged_docs_are_not_reported() -> None:
    same = {1: doc(1, "甲")}
    assert compute_changes(snapshot_of(same), snapshot_of(same)).empty is True


def test_dir_change_alone_counts_as_change() -> None:
    """文档被挪到别处，对 agent 是有效信息（该不该处理会变）。"""
    prev = snapshot_of({1: doc(1, "甲", dir="0921-0927")})
    cur = snapshot_of({1: doc(1, "甲", dir="归档区/0914-0920")})
    assert compute_changes(prev, cur).updated != []


def test_toc_change_is_recorded_but_does_not_alone_wake_the_agent() -> None:
    """目录结构变化会被**记下来**，但单靠它不足以唤醒 LLM。

    因为新建一篇文档必然会在目录里加一个节点——如果 ``toc_changed`` 也算「有变化」，
    那「占位标题过滤」「正文哈希过滤」全部会被目录变化抵消掉。
    纯目录变化（建空分组、调顺序）交给每周六的归档会话。
    """
    prev = snapshot_of({}, toc_sha="a")
    cur = snapshot_of({}, toc_sha="b")
    changes = compute_changes(prev, cur)
    assert changes.toc_changed is True, "结构变化要能被察觉"
    assert changes.empty is True, "但光结构变化不该唤醒 LLM"


# ---------------------------------------------------------------- 正文哈希过滤器


def test_updated_at_only_change_is_filtered_out_by_content_hash() -> None:
    """核心回归：语雀 bump 了 updated_at 但正文一字未改 → 不算变更。"""
    body = "正文没变"
    real_sha = _content_sha("甲", body)
    prev = snapshot_of({1: doc(1, "甲", updated_at="t1", sha=real_sha)})
    cur = snapshot_of({1: doc(1, "甲", updated_at="t2")})
    client = FakeYuque(doc_metas=[make_meta(1, "甲")], bodies={1: body})

    changes = compute_changes(prev, cur)
    assert changes.updated, "第一道筛子（updated_at）应该先把它捞出来"

    previews = enrich_and_refine(client, prev, cur, changes)  # type: ignore[arg-type]

    assert changes.updated == [], "正文哈希一致 → 必须丢掉"
    assert [d.doc_id for d in changes.toc_only] == [1]
    assert changes.empty is True, "既然不是真变更，就不该唤醒 LLM"
    assert previews[1] == body


def test_real_content_change_survives_the_hash_filter() -> None:
    prev = snapshot_of({1: doc(1, "甲", updated_at="t1", sha=_content_sha("甲", "旧正文"))})
    cur = snapshot_of({1: doc(1, "甲", updated_at="t2")})
    client = FakeYuque(doc_metas=[make_meta(1, "甲")], bodies={1: "正文真的改了"})

    changes = compute_changes(prev, cur)
    enrich_and_refine(client, prev, cur, changes)  # type: ignore[arg-type]

    assert [n.doc_id for _, n in changes.updated] == [1]
    assert changes.toc_only == []


def test_title_change_counts_as_content_change() -> None:
    """标题就是活动名称，改名是实质变更。"""
    body = "同样的正文"
    prev = snapshot_of({1: doc(1, "甲", updated_at="t1", sha=_content_sha("甲", body))})
    cur = snapshot_of({1: doc(1, "乙", updated_at="t2")})
    client = FakeYuque(doc_metas=[make_meta(1, "乙")], bodies={1: body})
    changes = compute_changes(prev, cur)
    enrich_and_refine(client, prev, cur, changes)  # type: ignore[arg-type]
    assert [n.doc_id for _, n in changes.updated] == [1]


def test_baseline_run_seeds_every_hash() -> None:
    """首次运行把所有正文哈希烧热，之后每次判定才有参照。"""
    cur = snapshot_of({1: doc(1, "甲"), 2: doc(2, "乙")})
    client = FakeYuque(doc_metas=[make_meta(1, "甲"), make_meta(2, "乙")], bodies={1: "a", 2: "b"})
    enrich_and_refine(client, None, cur, compute_changes(None, cur))  # type: ignore[arg-type]
    assert cur.docs[1].content_sha256 and cur.docs[2].content_sha256


def test_hashes_carry_over_when_nothing_changed() -> None:
    from yuque_agent.snapshot import _content_sha

    real = _content_sha("甲", "x")
    prev = snapshot_of({1: doc(1, "甲", sha=real)})
    cur = snapshot_of({1: doc(1, "甲")})
    client = FakeYuque(doc_metas=[make_meta(1, "甲")], bodies={1: "x"})
    enrich_and_refine(client, prev, cur, compute_changes(prev, cur))  # type: ignore[arg-type]
    assert cur.docs[1].content_sha256 == real
    assert client.calls == [], "没变就不该多读一次正文"


def test_read_failure_does_not_abort_the_round() -> None:
    from yuque_agent.snapshot import _content_sha

    prev = snapshot_of({1: doc(1, "甲", updated_at="t1", sha=_content_sha("甲", "x"))})
    cur = snapshot_of({1: doc(1, "甲", updated_at="t2")})
    client = FakeYuque(doc_metas=[make_meta(1, "甲")], error_on_doc={1})
    changes = compute_changes(prev, cur)
    enrich_and_refine(client, prev, cur, changes)  # type: ignore[arg-type]
    # 读不到就保守保留，不丢信息
    assert [n.doc_id for _, n in changes.updated] == [1]


# ---------------------------------------------------------------- 报告本身


def test_report_contains_no_judgement_fields() -> None:
    """报告只能有客观事实——出现 draft / valid / ok 这类字段就是越界了。"""
    cur = snapshot_of({1: doc(1, "新生见面会")})
    client = FakeYuque(doc_metas=[make_meta(1, "新生见面会")], bodies={1: "申请人：张三"})
    changes = compute_changes(snapshot_of({}), cur)
    report = build_report(
        run_id="r1",
        kind="polling",
        client=client,
        repo="g/kb",
        cur=cur,
        changes=changes,  # type: ignore[arg-type]
    )
    forbidden = {"is_draft", "valid", "judgement", "verdict", "rejected"}
    assert not (forbidden & set(report))
    flat = json.dumps(report, ensure_ascii=False)
    for word in forbidden:
        assert f'"{word}"' not in flat


def test_report_preview_is_truncated_with_a_hint() -> None:
    cur = snapshot_of({1: doc(1, "长文档")})
    body = "甲" * 900
    client = FakeYuque(doc_metas=[make_meta(1, "长文档")], bodies={1: body})
    changes = compute_changes(snapshot_of({}), cur)
    previews = enrich_and_refine(client, None, cur, changes)  # type: ignore[arg-type]
    report = build_report(
        run_id="r1",
        kind="polling",
        client=client,
        repo="g/kb",
        cur=cur,
        changes=changes,
        previews=previews,  # type: ignore[arg-type]
    )
    preview = report["docs"]["added"][0]["preview"]
    assert len(preview) < len(body)
    assert "doc_read" in preview


def test_report_lists_removed_without_reading_them() -> None:
    prev = snapshot_of({1: doc(1, "被删掉的")})
    cur = snapshot_of({})
    client = FakeYuque()
    changes = compute_changes(prev, cur)
    report = build_report(
        run_id="r1",
        kind="polling",
        client=client,
        repo="g/kb",
        cur=cur,
        changes=changes,  # type: ignore[arg-type]
    )
    assert report["docs"]["removed"][0]["doc_id"] == 1
    assert client.calls == []


def test_report_budget_truncation_is_announced() -> None:
    docs = {i: doc(i, f"文档{i}") for i in range(1, 6)}
    cur = snapshot_of(docs)
    client = FakeYuque(
        doc_metas=[make_meta(i, f"文档{i}") for i in docs], bodies={i: "x" for i in docs}
    )
    changes = compute_changes(snapshot_of({}), cur)
    previews = enrich_and_refine(client, None, cur, changes)  # type: ignore[arg-type]
    report = build_report(
        run_id="r1",
        kind="polling",
        client=client,
        repo="g/kb",
        cur=cur,
        changes=changes,
        max_docs=2,
        previews=previews,  # type: ignore[arg-type]
    )
    assert len(report["docs"]["added"]) == 2
    assert report["counts"]["added"] == 5
    assert any("max_docs" in n for n in report["notes"])


# ---------------------------------------------------------------- 采集


def test_take_snapshot_maps_docs_to_their_directory() -> None:
    toc = make_toc(
        ("0921-0927", "TITLE", 0, ""),
        ("新生见面会", "DOC", 11, "0921-0927"),
        ("归档区", "TITLE", 0, ""),
    )
    client = FakeYuque(toc_nodes=toc, doc_metas=[make_meta(11, "新生见面会")])
    snap = take_snapshot(client)  # type: ignore[arg-type]
    assert snap.docs[11].dir == "0921-0927"
    assert snap.toc_sha


def test_take_snapshot_toc_sha_changes_when_structure_changes() -> None:
    meta = [make_meta(11, "新生见面会")]
    a = take_snapshot(
        FakeYuque(
            toc_nodes=make_toc(
                ("0921-0927", "TITLE", 0, ""), ("新生见面会", "DOC", 11, "0921-0927")
            ),
            doc_metas=meta,
        )
    )  # type: ignore[arg-type]
    b = take_snapshot(
        FakeYuque(
            toc_nodes=make_toc(
                ("0921-0927", "TITLE", 0, ""),
                ("归档区", "TITLE", 0, ""),
                ("新生见面会", "DOC", 11, "0921-0927"),
            ),
            doc_metas=meta,
        )
    )  # type: ignore[arg-type]
    assert a.toc_sha != b.toc_sha
