"""`dir_list` / `kb_tree`：看知识库现状的两个读工具。

**`dir_list` 曾经有一个危险的 bug**（真机部署时抓到的）：
过滤条件写成 ``path == wanted or path.endswith("/" + wanted) or path == ""``，
最后那个 ``path == ""`` 让**根目录下的文档匹配任意目录**——于是
``dir_list("归档区/0912-0918")`` 会把根目录的《指导文档》《Agent 通知》一并返回。

危险之处在于归档会话：LLM 若信了这个结果，就会以为这两篇系统性文档在归档区，
**可能把它们从根目录搬走**。当时那轮归档会话自己察觉到了异常（它的思考里写着
"dir_list seems to return the same docs regardless? That's odd"），才没有出事。

**`kb_tree` 曾经名不副实**（2026-09-27 正式迁移实测）：它只列目录节点、
而且读的是**开跑时的快照**（从不重读）——而归档提示词要求「排完顺序后再读一遍核对」，
根目录那四项里前两项是文档、且顺序是硬要求。等于让 agent 对着旧数据核对。
现在它至少要做到两件事：**列全部节点（含文档）**、**读调用那一刻的真实目录**。
"""

from __future__ import annotations

import pytest

from tests.fakes import Ctx, make_meta
from yuque_agent import tools
from yuque_agent.config import Settings
from yuque_agent.yuque import TocNode


def toc_node(
    node_type: str,
    title: str,
    path: str,
    doc_id: int = 0,
    depth: int = 1,
    parent: str = "",
) -> dict:
    """目录树节点——**形状要和变更报告里的 ``toc`` 一致**（dict，不是 TocNode）。

    `_doc_dirs` 靠 **`parent_uuid`** 算「文档在哪个目录」，
    而不是去切 `path` 字符串（标题里可以带 `/`，切不得）。
    """
    return {
        "uuid": f"u-{title}",
        "type": node_type,
        "title": title,
        "depth": depth,
        "path": path,
        "parent_uuid": f"u-{parent}" if parent else "",
        "doc_id": doc_id,
    }


@pytest.fixture()
def ctx(tmp_path) -> Ctx:
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws")
    settings.ensure_dirs()
    toc = [
        # 根目录下的两篇系统性文档（dir 应当是空串）
        toc_node("DOC", "指导文档（必读）", "指导文档（必读）", 100),
        toc_node("DOC", "Agent 通知", "Agent 通知", 101),
        toc_node("TITLE", "0919-0925", "0919-0925"),
        toc_node("DOC", "新生见面会", "0919-0925/新生见面会", 1, depth=2, parent="0919-0925"),
        # 标题里带 `/` 的文档（真机上真的出现过）
        toc_node("DOC", "带/斜杠的标题", "0919-0925/带/斜杠的标题", 3, depth=2, parent="0919-0925"),
        toc_node("TITLE", "归档区", "归档区"),
        toc_node("TITLE", "0912-0918", "归档区/0912-0918", depth=2, parent="归档区"),
        toc_node("DOC", "读书会", "归档区/0912-0918/读书会", 2, depth=3, parent="0912-0918"),
    ]
    metas = [
        make_meta(100, "指导文档（必读）", updated_at="2026-09-20T01:00:00Z"),
        make_meta(101, "Agent 通知", updated_at="2026-09-20T02:00:00Z"),
        make_meta(1, "新生见面会", updated_at="2026-09-20T03:00:00Z"),
        make_meta(2, "读书会", updated_at="2026-09-20T04:00:00Z"),
        make_meta(3, "带/斜杠的标题", updated_at="2026-09-20T05:00:00Z"),
    ]
    # 假知识库也要有一份「实时目录」：`kb_tree` 读的是它（`dir_list` 读上面的 toc）。
    toc_nodes = [
        TocNode(
            uuid=str(node["uuid"]),
            type=str(node["type"]),
            title=str(node["title"]),
            doc_id=int(node["doc_id"]),
            slug=f"s{node['doc_id']}" if node["doc_id"] else "",
            parent_uuid=str(node["parent_uuid"]),
            depth=int(node["depth"]),
            path=str(node["path"]),
            order=index,
        )
        for index, node in enumerate(toc)
    ]
    return Ctx.build(settings, toc=toc, client_kwargs={"doc_metas": metas, "toc_nodes": toc_nodes})


def titles(result: dict) -> set[str]:
    return {d["title"] for d in result["docs"]}


def test_lists_only_docs_in_that_directory(ctx: Ctx) -> None:
    result = tools.execute(ctx.ctx, "dir_list", {"dir": "0919-0925"})
    assert result["ok"] is True
    assert titles(result["result"]) == {"新生见面会", "带/斜杠的标题"}, (
        "根目录的文档不该出现在周期目录里（曾经会）"
    )


def test_title_containing_a_slash_does_not_break_the_dir(ctx: Ctx) -> None:
    """**回归**：文档标题里带 ``/`` 时，不能靠切 ``path`` 字符串算目录。

    真机上真出现过：有人把文档改名成「测试1（我不申请了/(ㄒoㄒ)/~~）」，
    于是 ``path.split("/")[:-1]`` 得到的目录是
    ``0919-0925/测试1（我不申请了/(ㄒoㄒ)`` —— 这篇文档从此谁问都查不到，
    `yqa reset-test-data` 也因此漏掉了它。
    现在改为看 ``parent_uuid``，与 :func:`yuque.doc_dir_map` 同一套算法。
    """
    dirs = tools._doc_dirs(ctx.ctx)
    assert dirs[3] == "0919-0925", f"带斜杠的标题被算到了 {dirs[3]!r}"
    result = tools.execute(ctx.ctx, "dir_list", {"dir": "0919-0925"})
    assert "带/斜杠的标题" in titles(result["result"])


def test_nested_dir_does_not_leak_root_docs(ctx: Ctx) -> None:
    """**这条就是那个 bug 的回归测试。**"""
    result = tools.execute(ctx.ctx, "dir_list", {"dir": "归档区/0912-0918"})
    assert result["ok"] is True
    assert titles(result["result"]) == {"读书会"}, (
        "归档目录里只该有归档区的文档；混进《指导文档》《Agent 通知》会诱导归档会话把系统性文档搬走"
    )


def test_leaf_name_still_works(ctx: Ctx) -> None:
    """不带父目录的写法（'0912-0918'）仍然有效——这是有意留的便利。"""
    result = tools.execute(ctx.ctx, "dir_list", {"dir": "0912-0918"})
    assert titles(result["result"]) == {"读书会"}


def test_can_list_the_root_explicitly(ctx: Ctx) -> None:
    """根目录下的文档要能单独列出来（用 '.'）。"""
    for token in (".", "根目录"):
        result = tools.execute(ctx.ctx, "dir_list", {"dir": token})
        assert titles(result["result"]) == {"指导文档（必读）", "Agent 通知"}, token


def test_unknown_dir_returns_nothing_not_everything(ctx: Ctx) -> None:
    result = tools.execute(ctx.ctx, "dir_list", {"dir": "0926-1002"})
    assert result["result"]["count"] == 0


def test_missing_dir_is_an_error(ctx: Ctx) -> None:
    result = tools.execute(ctx.ctx, "dir_list", {})
    assert result["ok"] is False
    assert "dir 必填" in result["error"]


# ---------------------------------------------------------------- kb_tree


def test_kb_tree_lists_every_node_including_docs(ctx: Ctx) -> None:
    """目录树要**列出全部节点**（含文档节点）。

    归档的硬要求是根目录顺序 = 指导文档 → Agent 通知 → 当前周期 → 归档区，
    **前两项是文档**：只列目录节点的话，「排完再读一遍核对」看不到那两项。
    """
    result = tools.execute(ctx.ctx, "kb_tree", {})["result"]
    pairs = [(n["type"], n["title"]) for n in result["nodes"]]
    assert ("DOC", "指导文档（必读）") in pairs
    assert ("DOC", "Agent 通知") in pairs
    assert ("TITLE", "归档区") in pairs
    assert result["total_docs"] == 5, "文档总数要按实时目录算"
    # 顺序就是知识库里的顺序（根目录那两篇在最前）
    assert [n["title"] for n in result["nodes"]][:2] == ["指导文档（必读）", "Agent 通知"]


def test_kb_tree_docs_here_counts_only_directories(ctx: Ctx) -> None:
    by_title = {n["title"]: n for n in tools.execute(ctx.ctx, "kb_tree", {})["result"]["nodes"]}
    assert by_title["0919-0925"]["docs_here"] == 2
    assert by_title["0912-0918"]["docs_here"] == 1
    assert by_title["归档区"]["docs_here"] == 0
    assert by_title["指导文档（必读）"]["docs_here"] is None, "文档节点没有「目录下有几篇」"


def test_kb_tree_reads_the_live_tree_not_the_run_snapshot(ctx: Ctx) -> None:
    """**实时读取**：要看得见 agent 自己刚做过的改动。

    2026-09-27 真机实测：agent 建完目录再 `kb_tree()` 看到的还是开跑时的旧树
    （`nodes: []`），只能在总结里声明「工具异常、改用 dir_list 推理顺序」。
    这条用「改假知识库、再调一次」证明读的是**调用那一刻**的目录。
    """
    client = ctx.ctx.client
    before = tools.execute(ctx.ctx, "kb_tree", {})["result"]
    assert all(n["title"] != "0926-1002" for n in before["nodes"])

    client.toc_nodes = [  # type: ignore[attr-defined]  # 模拟 agent 刚建好新周期目录
        *client.toc_nodes,
        TocNode(
            uuid="u-new-cycle",
            type="TITLE",
            title="0926-1002",
            doc_id=0,
            slug="",
            parent_uuid="",
            depth=1,
            path="0926-1002",
            order=99,
        ),
    ]
    after = tools.execute(ctx.ctx, "kb_tree", {})["result"]
    assert any(n["title"] == "0926-1002" for n in after["nodes"]), (
        "知识库改了却看不到变化——那「再读一遍核对」就是骗人的"
    )
