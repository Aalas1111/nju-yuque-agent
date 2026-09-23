"""安全边界测试——**这是这个项目最重要的一组断言**。

项目的研究题目是「如何做出误操作最少、最安全的 LLM-agent」，
而本项目的答案是「用能力边界兜底，而不是用提示词求自律」。
所以下面这几条必须被测试锁住：**日常轮询会话里，改语雀的工具必须不存在。**
"""

from __future__ import annotations

import pathlib
import re

import pytest

from tests.fakes import Ctx
from yuque_agent import tools
from yuque_agent.config import Settings, safe_join
from yuque_agent.outputs import LLM_NOTICE_KINDS, NOTICE_KINDS, PROGRAM_NOTICE_KINDS
from yuque_agent.prompts import PromptLoader

#: 全部工具名（用于扫提示词里提到了哪些）
ALL_TOOL_NAMES = set(tools.tool_names("archive"))


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings(repo="g/kb", workspace=tmp_path / "ws")
    s.ensure_dirs()
    return s


@pytest.fixture()
def env(settings: Settings) -> Ctx:
    return Ctx.build(settings)


# ---------------------------------------------------------------- 能力分级


def test_polling_registers_no_yuque_write_tools() -> None:
    names = set(tools.tool_names("polling"))
    forbidden = {"toc_create", "toc_move", "toc_remove", "doc_create", "doc_delete"}
    assert not (names & forbidden), f"日常轮询会话竟然注册了写工具：{names & forbidden}"


def test_archive_registers_write_tools() -> None:
    names = set(tools.tool_names("archive"))
    expected = {"toc_create", "toc_move", "toc_remove", "doc_create", "doc_delete"}
    assert expected <= names


def test_unknown_run_kind_falls_back_to_common_only() -> None:
    """任何没见过的 kind 都退化成「只能读」，绝不意外放出写能力。"""
    assert set(tools.tool_names("weird-kind")) == set(tools.tool_names("polling"))


def test_execute_refuses_unregistered_tool(env: Ctx) -> None:
    """即使 LLM 硬报一个没注册的工具名，也会被拒绝（错误回给 LLM 自己纠正）。"""
    result = tools.execute(env.ctx, "doc_delete", {"doc_id": 1, "reason": "手滑"})
    assert result["ok"] is False
    assert "没有注册" in result["error"]


# ---------------------------------------------------------------- 工作区沙箱


def test_ws_write_lands_in_notes(env: Ctx, settings: Settings) -> None:
    result = tools.execute(env.ctx, "ws_write", {"path": "notes/remember.md", "content": "hello"})
    assert result["ok"] is True
    assert (settings.notes_dir / "remember.md").read_text(encoding="utf-8") == "hello"


@pytest.mark.parametrize(
    "evil",
    ["../escape.md", "notes/../../escape.md", "/etc/passwd", "notes/sub/../../../x.md"],
)
def test_ws_write_rejects_every_escape(env: Ctx, settings: Settings, evil: str) -> None:
    tools.execute(env.ctx, "ws_write", {"path": evil, "content": "x"})
    # 不管这个路径最终是被拒了还是被规整进 notes/，都绝不能在工作区外留下文件
    outside = [p for p in settings.workspace.parent.rglob("*.md") if settings.root not in p.parents]
    assert outside == [], f"{evil!r} 把文件写到工作区外了：{outside}"


def test_ws_write_escape_is_refused(env: Ctx) -> None:
    result = tools.execute(env.ctx, "ws_write", {"path": "../escape.md", "content": "x"})
    assert result["ok"] is False
    assert "notes" in result["error"] or "越界" in result["error"]


def test_safe_join_blocks_traversal(tmp_path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError):
        safe_join(root, "../outside.txt")
    assert safe_join(root, "a/b.txt") == (root / "a" / "b.txt").resolve()


def test_ws_read_cannot_read_outside_workspace(env: Ctx) -> None:
    result = tools.execute(env.ctx, "ws_read", {"path": "../../../etc/hosts"})
    assert result["ok"] is False


# ---------------------------------------------------------------- 产出契约


def test_emit_notice_rejects_bad_kind(env: Ctx) -> None:
    result = tools.execute(
        env.ctx, "emit_notice", {"kind": "whatever", "summary": "s", "message": "m"}
    )
    assert result["ok"] is False
    assert "kind" in result["error"]


def test_emit_notice_requires_message(env: Ctx) -> None:
    result = tools.execute(env.ctx, "emit_notice", {"kind": "rejected", "summary": "s"})
    assert result["ok"] is False


def test_emit_application_requires_essential_fields(env: Ctx) -> None:
    result = tools.execute(env.ctx, "emit_application", {"doc_id": 1, "campus": "仙林"})
    assert result["ok"] is False


def test_done_marks_context_finished(env: Ctx) -> None:
    assert env.ctx.finished is False
    tools.execute(env.ctx, "done", {"verdict": "nothing_to_do", "summary": "没事"})
    assert env.ctx.finished is True
    assert env.ctx.verdict == "nothing_to_do"


# ---------------------------------------------------------------- dry-run


def test_dry_run_archive_tools_do_not_touch_yuque(settings: Settings) -> None:
    """--dry-run 时归档工具只能「记录意图」，一个真实调用都不许发出去。"""
    settings.dry_run = True
    env = Ctx.build(settings, kind="archive")
    for name, args in (
        ("toc_create", {"title": "0928-1004"}),
        ("toc_move", {"node_uuid": "u1", "target_uuid": "u2"}),
        ("doc_delete", {"doc_id": 5, "reason": "空文档"}),
    ):
        result = tools.execute(env.ctx, name, args)
        assert result["ok"] is True
        assert result["result"]["dry_run"] is True
    assert env.ctx.client.calls == []  # 关键：没有真实调用
    assert len(env.ctx.kb_writes) == 3  # 但意图被记下来了


def test_doc_delete_requires_reason(settings: Settings) -> None:
    env = Ctx.build(settings, kind="archive")
    result = tools.execute(env.ctx, "doc_delete", {"doc_id": 5})
    assert result["ok"] is False
    assert "reason" in result["error"]


# ---------------------------------------------------------------- 文档漂移守卫


def test_prompts_only_mention_registered_tools() -> None:
    """提示词里提到的工具必须**真的在本轮注册**了。

    为什么要有这条：提示词和工具注册表是两份各自演进的产物，
    很容易漂移成「文档里写着某个工具、代码里根本没注册」——
    LLM 会去调一个不存在的工具，而我们得靠报错才知道。
    （同类漂移还有「设计文档写着某个字段/文件，代码里已经改了名字」，
    那种只能靠人工核对；至少工具这一项可以自动守住。）
    """
    pattern = re.compile(r"`([a-z_]+)(?:\(|`)")
    for kind in ("polling", "archive"):
        text = PromptLoader().load(kind)
        mentioned = {name for name in pattern.findall(text) if name in ALL_TOOL_NAMES}
        allowed = set(tools.tool_names(kind))
        assert not (mentioned - allowed), (
            f"{kind} 提示词提到了本轮不注册的工具：{sorted(mentioned - allowed)}"
        )


def test_every_registered_tool_has_a_description_and_schema() -> None:
    """每个工具都必须有描述与参数表——否则 LLM 不知道怎么用。"""
    for kind in ("polling", "archive"):
        for tool in tools.tools_for(kind):
            assert tool.description.strip(), f"{tool.name} 没有描述"
            schema = tool.schema()
            assert schema["function"]["name"] == tool.name
            assert schema["function"]["parameters"]["type"] == "object"


def test_prompt_tells_the_llm_about_every_notice_kind() -> None:
    """契约里承诺的每一种通知，提示词都必须交代怎么用。

    **这条守卫是真机部署时补的**：`handoff.md` 的契约词汇表里有 ``accepted``，
    《指导文档》也向社员承诺了「受理了会收到 QQ」，但 ``prompts/polling.md`` 里
    **一次都没提过 accepted**（grep 零命中），`emit_application` 也不会顺带发通知。
    结果：社员申请被受理后**收不到任何消息**，而指导文档刚跟他承诺过会收到。

    这类「契约里有、提示词里没有」的漂移不会报错，只会静默地少发消息。
    """
    text = PromptLoader().load("polling")
    missing = [kind for kind in LLM_NOTICE_KINDS if kind not in text]
    assert not missing, (
        f"提示词没交代这些 kind，LLM 永远不会产出它们：{missing}\n"
        f"（契约见 outputs.LLM_NOTICE_KINDS 与 docs/handoff.md §3.3）"
    )


def test_program_only_notice_kinds_are_not_in_the_prompt() -> None:
    """程序专属的 kind **不能**出现在提示词里。

    提示词里出现一个 LLM 根本发不出的 kind，就是在教它去调用一个会被拒的工具。
    所以两边的边界都要卡：契约要能发、提示词要交代、而程序专属的绝对不提。
    """
    text = PromptLoader().load("polling")
    leaked = [kind for kind in PROGRAM_NOTICE_KINDS if kind in text]
    assert not leaked, (
        f"这些 kind 是程序自己发的，不该出现在提示词里：{leaked}\n"
        f"（它们不在 outputs.LLM_NOTICE_KINDS 里，emit_notice 会直接拒掉）"
    )


def test_llm_cannot_emit_program_only_kinds() -> None:
    """能力闸门：工具描述里不能出现程序专属的 kind，而且得是真拒。

    光靠提示词叮嘱「别发 plan_updated」不算防守——这里查的是机制：
    ① 工具 schema 的枚举是从 ``LLM_NOTICE_KINDS`` 推的；
    ② ``emit_notice`` 处理器真拒。
    """
    schema = tools.tool_schemas("polling")
    notice = next(t for t in schema if t["function"]["name"] == "emit_notice")
    described = notice["function"]["parameters"]["properties"]["kind"]["description"]
    for kind in PROGRAM_NOTICE_KINDS:
        assert kind not in described, f"工具描述里混进了程序专属 kind：{kind}"
    for kind in LLM_NOTICE_KINDS:
        assert kind in described, f"工具描述里少了 {kind}（否则 LLM 不知道能填它）"

    assert set(NOTICE_KINDS) == set(LLM_NOTICE_KINDS) | set(PROGRAM_NOTICE_KINDS)


def test_accepted_notice_is_tied_to_emit_application() -> None:
    """受理必须**同时**产申请 + 发通知。

    只发 `emit_application` 不发 `accepted` 通知，社员就什么也收不到——
    这正是部署时踩到的那个缺口。
    """
    text = PromptLoader().load("polling")
    line = next(
        (ln for ln in text.splitlines() if "emit_application" in ln and "可以受理" in ln), None
    )
    assert line is not None, "提示词里找不到「可以受理」的产出说明"
    assert "emit_notice" in line and "accepted" in line, (
        f"「可以受理」那一行必须同时要求发 accepted 通知，实际：{line.strip()}"
    )


def test_guide_promises_match_what_the_system_can_do() -> None:
    """《指导文档》对社员的承诺，必须是系统真能做到的。

    指导文档里写着「要素齐全 → QQ：『已受理』+ 时间/校区」——这就是 `accepted`
    通知存在的理由。这条断言把「对社员的承诺」和「代码/提示词的能力」连起来。
    """
    import yuque_agent

    guide = (pathlib.Path(yuque_agent.__file__).parent / "kb" / "guide.md").read_text(
        encoding="utf-8"
    )
    assert "已受理" in guide, "指导文档应当告诉社员会被通知"
    assert "accepted" in NOTICE_KINDS, "指导文档承诺了已受理通知，契约里就必须有"
    assert "accepted" in PromptLoader().load("polling"), (
        "指导文档承诺了通知，提示词就必须让 LLM 真的发出来"
    )


def test_tool_grouping_is_frozen() -> None:
    """把工具清单冻住：以后加/删工具，必须同时改这条测试与设计文档 §5。"""
    assert tools.tool_names("polling") == [
        "kb_tree",
        "dir_list",
        "doc_read",
        "ws_list",
        "ws_read",
        "ws_write",
        "emit_application",
        "emit_notice",
        "done",
    ]
    assert tools.tool_names("archive") == tools.tool_names("polling") + [
        "toc_create",
        "toc_move",
        "toc_remove",
        "doc_create",
        "doc_delete",
    ]
