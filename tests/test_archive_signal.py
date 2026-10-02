"""归档终点那道筛子：**那里的改动不叫醒 LLM**。

2026-09-27 负责人拍板，原话大意：归档区反正 agent 啥事也不干，那唤醒干啥；
结构性问题留给每周六的归档会话（它另有一条完整目录树，不看变更列表）。

**草稿不在此列**——草稿在工作目录里、且「标记删了一半算不算草稿」是人对人的模糊语义，
那点 token 值得花（鲁棒性优先）。筛子的分工见 `docs/design.md` §8.3。
"""

from __future__ import annotations

import json
from datetime import timedelta

from tests.fakes import FakeLLM, FakeYuque, call, make_meta, make_toc
from tests.test_debounce import dt
from yuque_agent import cli
from yuque_agent.config import ARCHIVE_ZONE_TITLE, WORKING_DIR_TITLE, Settings
from yuque_agent.runner import Runner

T0 = dt("2026-09-20T10:00:00")


def make_kb(tmp_path) -> tuple[Runner, FakeYuque, FakeLLM]:
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws", quiet_seconds=0)
    settings.ensure_dirs()
    client = FakeYuque()
    llm = FakeLLM(script=[call("done", verdict="nothing_to_do", summary="无事")] * 6)
    runner = Runner(settings=settings, client=client, llm=llm)  # type: ignore[arg-type]
    return runner, client, llm


def set_kb(
    client: FakeYuque,
    *,
    active_updated: str = "t1",
    active_body: str = "申请人：张三\n活动日期：2026-09-30",
    archived_updated: str = "t1",
    archived_body: str = "上学期的东西",
    with_archived: bool = True,
) -> None:
    """工作目录里放一篇申请；归档终点里按需放一篇（用来验证它不产生信号）。"""
    nodes = [
        (WORKING_DIR_TITLE, "TITLE", 0, ""),
        ("活跃申请", "DOC", 1, WORKING_DIR_TITLE),
        (ARCHIVE_ZONE_TITLE, "TITLE", 0, ""),
    ]
    metas = [make_meta(1, "活跃申请", updated_at=active_updated)]
    bodies = {1: active_body}
    if with_archived:
        nodes += [("上学期活动", "DOC", 9, ARCHIVE_ZONE_TITLE)]
        metas.append(make_meta(9, "上学期活动", updated_at=archived_updated))
        bodies[9] = archived_body

    client.toc_nodes = make_toc(*nodes)
    client.doc_metas = metas
    client.bodies = bodies


def test_archive_zone_change_alone_does_not_wake_the_llm(tmp_path) -> None:
    runner, client, llm = make_kb(tmp_path)
    set_kb(client)
    runner.poll_once(now=T0)  # 基线（静默）

    set_kb(client, archived_updated="t2", archived_body="上学期的东西（被人改了一句）")

    assert runner.poll_once(now=T0 + timedelta(seconds=30)) is None
    assert llm.calls == 0, "归档里的改动不该叫醒 LLM——它在那里无事可做"
    assert runner.last_skip == "archived_only", "也不能报成「没有变化」（那是假话）"


def test_archive_zone_delete_alone_does_not_wake_the_llm(tmp_path) -> None:
    runner, client, llm = make_kb(tmp_path)
    set_kb(client)
    runner.poll_once(now=T0)

    set_kb(client, with_archived=False)  # 归档里那篇被删了

    assert runner.poll_once(now=T0 + timedelta(seconds=30)) is None
    assert llm.calls == 0


def test_archive_zone_change_is_noted_when_a_real_change_wakes_the_llm(tmp_path) -> None:
    """真有事发生时，报告里要说明「归档里那几篇被程序剔除了」——留痕，别让人猜。"""
    runner, client, llm = make_kb(tmp_path)
    set_kb(client)
    runner.poll_once(now=T0)

    set_kb(
        client,
        active_updated="t2",
        active_body="申请人：张三\n活动日期：2026-09-30\n人数：25",
        archived_updated="t2",
        archived_body="上学期的东西（被人改了一句）",
    )
    result = runner.poll_once(now=T0 + timedelta(seconds=30))

    assert result is not None and llm.calls == 1
    user = [m for m in llm.seen_messages[-1] if m.get("role") == "user"][-1]
    payload = json.loads(user["content"])
    docs = payload["docs"]
    titles = [d["title"] for d in docs["added"]] + [d["title"] for d in docs["updated"]]
    assert "活跃申请" in titles, "工作目录里的变更照旧交给 LLM"
    assert "上学期活动" not in titles, "归档里那篇不该出现在变更列表里"
    assert any(ARCHIVE_ZONE_TITLE in note and "剔除" in note for note in payload["notes"]), payload[
        "notes"
    ]


def test_archived_only_skip_is_not_reported_as_no_change(tmp_path) -> None:
    """`yqa once` 在「只有归档在动」时要说实话（同静默期那条的老规矩）。"""
    runner, client, llm = make_kb(tmp_path)
    settings = runner.settings

    runner.last_skip = "archived_only"
    text = cli._poll_skip_message(runner, settings)

    assert "没有变化" not in text, "只有归档变了 ≠ 知识库没有变化"
    assert ARCHIVE_ZONE_TITLE in text


# ---------------------------------------------------------------- 移动的信号（2026-10-02）


def test_move_into_working_dir_wakes_the_llm(tmp_path) -> None:
    """正文没变、只被移进工作目录 → **要**叫醒 LLM。

    这是老师事故的反面：文档放错目录、写完正文、挪回工作目录，以前会被
    「纯移动」筛子整条吞掉，从此再也没人处理。现在改成单列进 docs.moved。
    """
    runner, client, llm = make_kb(tmp_path)
    client.toc_nodes = make_toc(
        ("旧目录", "TITLE", 0, ""),
        ("申请", "DOC", 1, "旧目录"),
        (WORKING_DIR_TITLE, "TITLE", 0, ""),
    )
    client.doc_metas = [make_meta(1, "申请", updated_at="t1")]
    client.bodies = {1: "申请人：张三\n活动日期：2026-09-30"}
    assert runner.poll_once(now=T0) is None  # 基线（首次运行静默）

    client.toc_nodes = make_toc(
        (WORKING_DIR_TITLE, "TITLE", 0, ""),
        ("申请", "DOC", 1, WORKING_DIR_TITLE),
        ("旧目录", "TITLE", 0, ""),
    )
    client.doc_metas = [make_meta(1, "申请", updated_at="t2")]  # 移动会 bump updated_at

    result = runner.poll_once(now=T0 + timedelta(seconds=30))

    assert result is not None and llm.calls == 1, "移动要唤醒 LLM"
    user = [m for m in llm.seen_messages[-1] if m.get("role") == "user"][-1]
    payload = json.loads(user["content"])
    moved = payload["docs"]["moved"]
    assert len(moved) == 1
    assert moved[0]["prev_dir"] == "旧目录"
    assert moved[0]["dir"] == WORKING_DIR_TITLE


def test_move_into_archive_zone_does_not_wake_the_llm(tmp_path) -> None:
    """归档会话自己把文档搬进归档终点（正文没变）→ 不该叫醒 LLM。

    这是筛子的本职：归档是程序/归档会话做的批量搬家，轮询侧无事可做。
    """
    runner, client, llm = make_kb(tmp_path)
    client.toc_nodes = make_toc(
        (WORKING_DIR_TITLE, "TITLE", 0, ""),
        ("活跃申请", "DOC", 1, WORKING_DIR_TITLE),
        (ARCHIVE_ZONE_TITLE, "TITLE", 0, ""),
    )
    client.doc_metas = [make_meta(1, "活跃申请", updated_at="t1")]
    client.bodies = {1: "申请人：张三\n活动日期：2026-09-30"}
    runner.poll_once(now=T0)  # 基线

    client.toc_nodes = make_toc(
        (WORKING_DIR_TITLE, "TITLE", 0, ""),
        (ARCHIVE_ZONE_TITLE, "TITLE", 0, ""),
        ("活跃申请", "DOC", 1, ARCHIVE_ZONE_TITLE),
    )
    client.doc_metas = [make_meta(1, "活跃申请", updated_at="t2")]  # 搬家 bump 了时间戳

    assert runner.poll_once(now=T0 + timedelta(seconds=30)) is None
    assert llm.calls == 0, "搬进归档终点的文档不再处理，别叫醒 LLM"
