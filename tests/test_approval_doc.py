"""《审批结果》文档 + 它在根目录里的位置。

这份文档是**程序维护**的，内容取自下游 ``crb-notify`` 写在工作区里的
``outbox/approval/notifications.json``（本项目不查学校系统，见 docs/handoff.md §1.1）。

三条要守住的东西：

1. 渲染出来的正文必须**与语雀读回来的形式逐字一致**（否则每轮刷新都白写一次
   —— `noticedoc` 2026-09-26 实测踩过：多一个空行就刷屏）；
2. 它必须待在**《Agent 通知》与活跃周期目录之间**（需求方定的位置）；
3. 它的变更**永远不算「知识库变了」**（`ignore_doc_titles`），否则
   「程序写文档 → 文档变了 → 唤醒 LLM → 又写文档」会自激。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fakes import FakeYuque, make_toc
from yuque_agent import approvaldoc
from yuque_agent.config import APPROVAL_TITLE, GUIDE_TITLE, NOTICE_TITLE, Settings


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings(repo="g/kb", workspace=tmp_path / "ws")
    s.ensure_dirs()
    return s


def put(settings: Settings, notifications: list[dict], unmatched: list[dict] | None = None) -> None:
    """往工作区里放下游的产出（真的落文件，走读路径）。"""
    target = approvaldoc.approval_dir(settings)
    target.mkdir(parents=True, exist_ok=True)
    (target / "notifications.json").write_text(
        json.dumps(
            {
                "schemaVersion": "nova.classroom-borrow-notification.v1",
                "batchId": "notification-2026-09-27T10:30:00.000Z",
                "generatedAt": "2026-09-27T10:30:00.000Z",
                "notifications": notifications,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if unmatched is not None:
        (target / "unmatched.json").write_text(
            json.dumps({"generatedAt": "x", "unmatched": unmatched}, ensure_ascii=False),
            encoding="utf-8",
        )


def item(
    *,
    title: str = "如何设计一个可靠的 Agent",
    kind: str = "approved",
    rooms: list[str] | None = None,
    feedback: str = "",
    detected_at: str = "2026-09-27T10:25:30+08:00",
) -> dict:
    return {
        "notificationId": "notify-a13f52c8",
        "applicationId": "2026-10-10-286001001",
        "sqbh": "7e75e389f83d4aac95bdcc28b5521041",
        "type": kind,
        "activity": {
            "title": title,
            "date": "2026-10-10",
            "slotStart": "第5节(14:00-14:50)",
            "slotEnd": "第6节(15:00-16:50)",
            "campus": "鼓楼校区",
            "organizer": "张三",
            "sourceDoc": {"id": "286001001", "title": title, "repo": "g/kb", "dir": "1004-1010"},
        },
        "result": {
            "status": "approved_assigned" if kind == "approved" else "rejected",
            "actualRooms": rooms
            if rooms is not None
            else (["新教404"] if kind == "approved" else []),
            "feedback": feedback,
        },
        "detectedAt": detected_at,
    }


# ---------------------------------------------------------------- 渲染
def test_render_of_nothing_says_so(settings: Settings) -> None:
    text = approvaldoc.render(settings)
    assert "还没有审批结束的申请" in text
    assert "请勿手工编辑" in text


def test_render_approved_carries_the_room(settings: Settings) -> None:
    put(settings, [item()])
    text = approvaldoc.render(settings)
    assert "如何设计一个可靠的 Agent · 已通过" in text
    assert "教室：**新教404**" in text
    assert "鼓楼校区" in text
    assert "第5节(14:00-14:50) - 第6节(15:00-16:50)" in text


def test_render_rejected_carries_the_reason(settings: Settings) -> None:
    put(settings, [item(kind="rejected", feedback="申请时间不符合教室借用要求")])
    text = approvaldoc.render(settings)
    assert "已退回" in text
    assert "申请时间不符合教室借用要求" in text
    assert "教室：" not in text


def test_render_is_newest_first(settings: Settings) -> None:
    put(
        settings,
        [
            item(title="早的", detected_at="2026-09-27T10:00:00+08:00"),
            item(title="晚的", detected_at="2026-09-27T11:00:00+08:00"),
        ],
    )
    text = approvaldoc.render(settings)
    assert text.index("晚的") < text.index("早的")


def test_unmatched_never_reaches_the_document(settings: Settings) -> None:
    """认不出的**一个字都不许进正文**，连「有几条」都不许提。

    它是**运维要向**：可见处是 `crb-notify show` 与接口响应里的 `unmatched` 计数。
    《审批结果》是给社员看「批没批、哪间教室」的，塞内部状态进去只会让人困惑
    （需求方 2026-09-27 明确要求删掉那一句）。
    """
    put(settings, [item()], unmatched=[{"sqbh": "x", "why": "找不到"}])
    text = approvaldoc.render(settings)
    assert "认不出" not in text
    assert "找不到" not in text
    assert "unmatched" not in text


def test_render_leaves_no_blank_line_between_headings(settings: Settings) -> None:
    """标题行与下一行之间不留空行 —— 语雀读回来会把空行规范化掉。"""
    put(settings, [item()])
    lines = approvaldoc.render(settings).splitlines()
    for index, line in enumerate(lines):
        if line.startswith("## "):
            assert lines[index + 1].strip(), f"`{line}` 后面不该是空行"


def test_render_is_stable_across_calls(settings: Settings) -> None:
    """同样的输入渲染两次必须逐字相同（否则每轮刷新都会白写一次）。"""
    put(settings, [item()])
    assert approvaldoc.render(settings) == approvaldoc.render(settings)


# ---------------------------------------------------------------- 写语雀
def client_with_existing(body: str) -> FakeYuque:
    """「知识库里已经有这篇文档」的假件：docs() 能列到、doc() 能读回正文。"""
    from yuque_agent.yuque import DocDetail, DocMeta

    meta = DocMeta(
        doc_id=9,
        slug="approval-slug",
        title=APPROVAL_TITLE,
        updated_at="t",
        created_at="c",
        author="",
    )
    detail = DocDetail(**meta.__dict__, body=body)
    client = FakeYuque(doc_metas=[meta])
    client.docs = lambda: [meta]  # type: ignore[method-assign]
    client.doc = lambda ref: detail  # type: ignore[method-assign]
    return client


def test_refresh_creates_the_doc_and_hangs_it_in_the_tree(settings: Settings) -> None:
    """文档不存在就建一个，并**挂进目录**（否则社员在侧边栏看不到）。"""
    put(settings, [item()])
    client = FakeYuque()
    client.docs = lambda: []  # type: ignore[method-assign]
    created = client.create_doc
    client.create_doc = lambda **kw: {**created(**kw), "id": 777}  # type: ignore[method-assign]

    outcome = approvaldoc.refresh(settings, client)

    assert outcome["ok"] and outcome["created"] is True and outcome["count"] == 1
    assert [op for op, _ in client.calls] == ["create_doc", "toc_add"]
    assert client.calls[0][1]["title"] == APPROVAL_TITLE
    assert client.calls[1][1]["doc_ids"] == [777]
    assert "新教404" in client.calls[0][1]["body"]


def test_refresh_only_writes_when_content_changed(settings: Settings) -> None:
    put(settings, [item()])
    stale = "旧的正文"
    client = client_with_existing(stale)
    outcome = approvaldoc.refresh(settings, client)
    assert outcome["ok"] and outcome["unchanged"] is False
    op, kwargs = client.calls[0]
    assert op == "update_doc"
    assert kwargs["doc_id"] == "approval-slug", "语雀只接受 slug，不能拿 doc_id 去更新"

    # 内容已经一致 → 一个字都不写（每轮都重建，省掉没意义的写操作）
    quiet = client_with_existing(approvaldoc.render(settings))
    again = approvaldoc.refresh(settings, quiet)
    assert again["unchanged"] is True
    assert quiet.calls == []


def test_refresh_does_not_write_in_dry_run(settings: Settings) -> None:
    put(settings, [item()])
    settings.dry_run = True
    client = client_with_existing("旧的正文")
    outcome = approvaldoc.refresh(settings, client)
    assert outcome["ok"] and outcome.get("dry_run") is True
    assert client.calls == [], "dry-run 一个写操作都不该发"


def test_missing_file_is_treated_as_no_results(settings: Settings) -> None:
    """下游还没跑过时，读不到文件**不是错误** —— 就是「还没有结束的申请」。"""
    assert approvaldoc.load_notifications(settings) == {}
    assert "还没有审批结束的申请" in approvaldoc.render(settings)


# ---------------------------------------------------------------- 位置与自激
def test_approval_doc_sits_between_the_notice_and_the_cycle(settings: Settings) -> None:
    """位置是需求方定的：《Agent 通知》之后、活跃周期目录之前。"""
    order = settings_archive_order(settings, cycle="0926-1002")
    assert order == [GUIDE_TITLE, NOTICE_TITLE, APPROVAL_TITLE, "0926-1002", "归档区"]


def test_the_archive_prompt_documents_the_new_order() -> None:
    """提示词里的根目录形态必须跟着变，否则归档会话会把它挪回去。"""
    text = (
        Path(__file__).resolve().parents[1] / "src" / "yuque_agent" / "prompts" / "archive.md"
    ).read_text(encoding="utf-8")
    assert "审批结果" in text
    assert "第 3 位" in text  # 审批结果的位置
    assert "五" in text  # 「这五条」


def test_the_step_that_reorders_does_not_carry_its_own_list() -> None:
    """重排那一步**不许**自己抄一份顺序，只能照抄指令里的 `root_target_order`。

    实测踩到（2026-09-27）：这一步抄的是**四项**（少了《审批结果》），而程序给的是五项
    —— 它照着排，正好把《审批结果》留在了归档区下面。抄一份就一定会漂。
    """
    text = (
        Path(__file__).resolve().parents[1] / "src" / "yuque_agent" / "prompts" / "archive.md"
    ).read_text(encoding="utf-8")
    step = text.split("5. **按", 1)[1].split("6. **", 1)[0]
    assert "root_target_order" in step, "要指明照哪个字段排"
    assert "<当前周期>" not in step, "别自己拼一份清单出来 —— 抄过一次就漂了"


# ---------------------------------------------------------------- 位置：程序自己摆
def root_order(client: FakeYuque) -> list[str]:
    return [node.title for node in client.toc_nodes if node.depth == 1]


def test_it_is_pulled_up_to_just_after_the_notice(settings: Settings) -> None:
    """实测踩到（2026-09-27）：它落在**归档区下面**。

    `create_doc` 只能把新文档追加到根目录末尾，而摆位置只有周期翻转那一刻才做 ——
    于是新建的《审批结果》会在最下面待一整周。位置是需求，所以程序自己摆。
    """
    client = client_with_existing(approvaldoc.render(settings))
    client.toc_nodes = make_toc(
        (GUIDE_TITLE, "DOC", 1, ""),
        (NOTICE_TITLE, "DOC", 2, ""),
        ("0926-1002", "TITLE", 0, ""),
        ("归档区", "TITLE", 0, ""),
        (APPROVAL_TITLE, "DOC", 9, ""),
    )

    outcome = approvaldoc.refresh(settings, client)

    assert outcome["ok"] is True
    assert outcome["moved"] == [GUIDE_TITLE, NOTICE_TITLE, APPROVAL_TITLE]
    assert root_order(client) == [GUIDE_TITLE, NOTICE_TITLE, APPROVAL_TITLE, "0926-1002", "归档区"]


def test_placement_is_a_no_op_when_the_order_is_already_right(settings: Settings) -> None:
    """顺序已经对了就一个字节都不写 —— 它每收一条结果都会跑一次，不能天天重排。"""
    client = client_with_existing(approvaldoc.render(settings))
    client.toc_nodes = make_toc(
        (GUIDE_TITLE, "DOC", 1, ""),
        (NOTICE_TITLE, "DOC", 2, ""),
        (APPROVAL_TITLE, "DOC", 9, ""),
        ("0926-1002", "TITLE", 0, ""),
        ("归档区", "TITLE", 0, ""),
    )

    outcome = approvaldoc.refresh(settings, client)

    assert outcome["ok"] is True and "moved" not in outcome
    assert client.calls == [], f"什么都不该写，实际写了 {client.calls}"


def test_placement_only_touches_the_front_of_the_root(settings: Settings) -> None:
    """别人放在根目录的散篇只是被挤到后面，**相对顺序不动**，归档区仍在最末。"""
    client = client_with_existing(approvaldoc.render(settings))
    client.toc_nodes = make_toc(
        (GUIDE_TITLE, "DOC", 1, ""),
        ("某人的散篇", "DOC", 8, ""),
        (NOTICE_TITLE, "DOC", 2, ""),
        ("0926-1002", "TITLE", 0, ""),
        ("归档区", "TITLE", 0, ""),
        (APPROVAL_TITLE, "DOC", 9, ""),
    )

    approvaldoc.refresh(settings, client)

    assert root_order(client) == [
        GUIDE_TITLE,
        NOTICE_TITLE,
        APPROVAL_TITLE,
        "某人的散篇",
        "0926-1002",
        "归档区",
    ]


def test_a_placement_failure_does_not_deny_the_body_write(settings: Settings) -> None:
    """摆位置失败**不能**把「正文已经写好了」说成失败 —— 那是两件事。"""
    from yuque_agent.yuque import YuqueError

    client = client_with_existing("旧的正文")
    client.toc = lambda: (_ for _ in ()).throw(YuqueError("模拟目录读取失败"))  # type: ignore

    outcome = approvaldoc.refresh(settings, client)

    assert outcome["ok"] is True
    assert outcome["unchanged"] is False, "正文确实写了"
    assert "模拟目录读取失败" in outcome["place_error"]


def test_approval_doc_changes_never_count_as_kb_changes(settings: Settings) -> None:
    """它的变更不算「知识库变了」——否则「程序写文档 → 唤醒 LLM → 又写」会自激。"""
    assert APPROVAL_TITLE in settings.ignore_doc_titles


def settings_archive_order(settings: Settings, *, cycle: str) -> list[str]:
    """拿归档指令里那份目标顺序（不跑 LLM，直接看程序算出来的）。

    顺序是**程序给的**（那是需求，不是判断）；``now`` 决定「活跃周期目录」叫什么，
    2026-09-27 是周日，落在周期 ``0926-1002``（周六起算）。
    """
    from datetime import datetime

    from yuque_agent import runner
    from yuque_agent.snapshot import Snapshot

    signal = runner.build_archive_instruction(
        settings=settings,
        snapshot=Snapshot(taken_at="2026-09-27T00:00:00+08:00"),
        now=datetime(2026, 9, 27, 10, 0),
    )
    order = list(signal["root_target_order"])
    assert cycle in order, f"活跃周期目录应该是 {cycle}，实际 {order}"
    return order
