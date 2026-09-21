"""`dir_list`：列出某目录下的文档。

**这个工具曾经有一个危险的 bug**（真机部署时抓到的）：
过滤条件写成 ``path == wanted or path.endswith("/" + wanted) or path == ""``，
最后那个 ``path == ""`` 让**根目录下的文档匹配任意目录**——于是
``dir_list("归档区/0912-0918")`` 会把根目录的《指导文档》《工作日志》一并返回。

危险之处在于归档会话：LLM 若信了这个结果，就会以为这两篇系统性文档在归档区，
**可能把它们从根目录搬走**。当时那轮归档会话自己察觉到了异常（它的思考里写着
"dir_list seems to return the same docs regardless? That's odd"），才没有出事。
"""

from __future__ import annotations

import pytest

from tests.fakes import Ctx, make_meta
from yuque_agent import tools
from yuque_agent.config import Settings


def toc_node(node_type: str, title: str, path: str, doc_id: int = 0, depth: int = 1) -> dict:
    """目录树节点——**这里的形状要和变更报告里的 ``toc`` 一致**（dict，不是 TocNode）。

    `_doc_dirs` 就是从它身上算「文档在哪个目录」的。
    """
    return {
        "uuid": f"u-{path}",
        "type": node_type,
        "title": title,
        "depth": depth,
        "path": path,
        "doc_id": doc_id,
    }


@pytest.fixture()
def ctx(tmp_path) -> Ctx:
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws")
    settings.ensure_dirs()
    toc = [
        # 根目录下的两篇系统性文档（dir 应当是空串）
        toc_node("DOC", "指导文档（必读）", "指导文档（必读）", 100),
        toc_node("DOC", "工作日志", "工作日志", 101),
        toc_node("TITLE", "0919-0925", "0919-0925"),
        toc_node("DOC", "新生见面会", "0919-0925/新生见面会", 1, depth=2),
        toc_node("TITLE", "归档区", "归档区"),
        toc_node("TITLE", "0912-0918", "归档区/0912-0918", depth=2),
        toc_node("DOC", "读书会", "归档区/0912-0918/读书会", 2, depth=3),
    ]
    metas = [
        make_meta(100, "指导文档（必读）", updated_at="2026-09-20T01:00:00Z"),
        make_meta(101, "工作日志", updated_at="2026-09-20T02:00:00Z"),
        make_meta(1, "新生见面会", updated_at="2026-09-20T03:00:00Z"),
        make_meta(2, "读书会", updated_at="2026-09-20T04:00:00Z"),
    ]
    return Ctx.build(settings, toc=toc, client_kwargs={"doc_metas": metas})


def titles(result: dict) -> set[str]:
    return {d["title"] for d in result["docs"]}


def test_lists_only_docs_in_that_directory(ctx: Ctx) -> None:
    result = tools.execute(ctx.ctx, "dir_list", {"dir": "0919-0925"})
    assert result["ok"] is True
    assert titles(result["result"]) == {"新生见面会"}, "根目录的文档不该出现在周期目录里（曾经会）"


def test_nested_dir_does_not_leak_root_docs(ctx: Ctx) -> None:
    """**这条就是那个 bug 的回归测试。**"""
    result = tools.execute(ctx.ctx, "dir_list", {"dir": "归档区/0912-0918"})
    assert result["ok"] is True
    assert titles(result["result"]) == {"读书会"}, (
        "归档目录里只该有归档区的文档；混进《指导文档》《工作日志》会诱导归档会话把系统性文档搬走"
    )


def test_leaf_name_still_works(ctx: Ctx) -> None:
    """不带父目录的写法（'0912-0918'）仍然有效——这是有意留的便利。"""
    result = tools.execute(ctx.ctx, "dir_list", {"dir": "0912-0918"})
    assert titles(result["result"]) == {"读书会"}


def test_can_list_the_root_explicitly(ctx: Ctx) -> None:
    """根目录下的文档要能单独列出来（用 '.'）。"""
    for token in (".", "根目录"):
        result = tools.execute(ctx.ctx, "dir_list", {"dir": token})
        assert titles(result["result"]) == {"指导文档（必读）", "工作日志"}, token


def test_unknown_dir_returns_nothing_not_everything(ctx: Ctx) -> None:
    result = tools.execute(ctx.ctx, "dir_list", {"dir": "0926-1002"})
    assert result["result"]["count"] == 0


def test_missing_dir_is_an_error(ctx: Ctx) -> None:
    result = tools.execute(ctx.ctx, "dir_list", {})
    assert result["ok"] is False
    assert "dir 必填" in result["error"]
