"""安全边界测试——**这是这个项目最重要的一组断言**。

项目的研究题目是「如何做出误操作最少、最安全的 LLM-agent」，
而本项目的答案是「用能力边界兜底，而不是用提示词求自律」。
所以下面这几条必须被测试锁住：**日常轮询会话里，改语雀的工具必须不存在。**

文件末尾还有一组**提示词文本守卫**（契约 ↔ 提示词漂没漂、提示词内部自相矛盾）。
为什么「测试守卫可以、程序闸门不行」，见 `docs/principles.md`。
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


def test_member_name_is_explained_in_both_places() -> None:
    """`member_name` 必须在**工具描述**和**提示词**里都交代。

    这条守卫来自真机问题：QQ 通知发给谁，全靠 LLM 把文档里「申请人：」搬进
    `member_name`，而——工具 schema 里它是个裸的 `STR`（没有 description），
    提示词里 `member_name` **零命中**。字段名自解释所以能用，但那是靠运气，
    而且漂了没人会发现（同 `accepted` 那次的毛病）。

    后果很具体：填错一个字 → 查不到 → 通知进 `unrouted/`，那位社员什么都收不到。
    """
    schema = tools.tool_schemas("polling")
    notice = next(t for t in schema if t["function"]["name"] == "emit_notice")
    param = notice["function"]["parameters"]["properties"]["member_name"]
    described = param.get("description") or ""
    assert described, "emit_notice.member_name 没有 description —— LLM 只能猜它是什么"
    assert "申请人" in described, f"描述里要说清它是文档里那个人名，实际：{described!r}"

    text = PromptLoader().load("polling")
    assert "member_name" in text, "提示词里没提 member_name（契约有、提示词不提=会漂）"
    assert "申请人" in text


def test_member_name_tells_the_llm_not_to_make_one_up() -> None:
    """文档里没写申请人时**不许编**。

    编一个名字的后果比留空严重：留空只进 `unrouted/`（可恢复），
    编出来可能命中另一个真实成员 → **通知发错人**。
    """
    text = PromptLoader().load("polling")
    assert "不要编" in text or "绝不要编" in text, "没告诉它「没写就留空、别编」"


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


def test_prompt_quotes_the_real_draft_marker() -> None:
    """提示词里引用的「草稿标记」必须与模板第一行**逐字一致**。

    模板是需求方会随时改的（2026-09-26 改过一次：`【草稿】填完请删掉这一行，agent 才会处理本文档`
    → `【草稿】填完请把这一行删掉`）。提示词里留着旧原文的话，LLM 会按字面去认一句话，
    而那句话已经不存在了——「草稿标记删了一半」这类判断就会走偏。
    """
    import yuque_agent

    template = (pathlib.Path(yuque_agent.__file__).parent / "kb" / "template.md").read_text(
        encoding="utf-8"
    )
    marker = template.splitlines()[0].strip()
    assert marker.startswith("【草稿】"), f"模板第一行不再像草稿标记了：{marker!r}"
    assert marker in PromptLoader().load("polling"), (
        f"提示词里没引用模板当前的草稿标记（{marker!r}）——模板改了，提示词要跟着改"
    )


def test_prompt_knows_the_template_title() -> None:
    """模板的标题（`请填写活动名称`）必须在提示词里被点名。

    从模板新建、又忘了改名的文档，标题就是它——**那是机器给的名字，不是活动名**。
    不点名的话，LLM 很可能照「标题就是活动名称」把它当成活动名报上去，
    cac 收到的申请就叫「请填写活动名称」。
    （标题是它、正文也是空的：那种更早一步，由 `Settings.placeholder_titles` 直接剔除。）
    """
    from yuque_agent.config import TEMPLATE_TITLE

    assert TEMPLATE_TITLE in PromptLoader().load("polling"), (
        f"提示词里没提模板标题（{TEMPLATE_TITLE!r}）——模板改名了？改 `config.TEMPLATE_TITLE` 时这里要一起改"
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


# ------------------------------------------------- 「文案规矩」的文本守卫
#
# 只守**提示词文本**，不守 LLM 的输出：在程序层做语义拦截是明确禁止的
# （硬字符串判断必然误伤，而且把「LLM 能不能自己守规矩」这个研究对象阉割掉）。
# 见 `docs/principles.md` §4。

#: 「让社员去改自己的文档」这类说法的典型写法。它们只允许出现在**文案规矩块**里
#: ——那一块在讲「不许这么说」，必须引用这些说法当反例。
TELL_MEMBER_TO_EDIT_PHRASES = ("把文档改一下", "改一下告诉我就行", "想改的话", "补充一下")


def _prompt_copy_rules_block(text: str) -> str:
    """截出提示词里「受理通知的文案规矩」那一块（📐 到下一个块 📮）。"""
    start = text.find("📐")
    end = text.find("📮")
    assert start != -1 and end > start, (
        "提示词里找不到文案规矩块（📐 … 📮）——要么规矩被删了，要么块的标记变了，"
        "这条守卫要跟着改（别直接删守卫）"
    )
    return text[start:end]


def test_polling_prompt_never_asks_members_to_edit_the_doc() -> None:
    """提示词里（除了规矩块本身）不许出现「让社员去改文档」的说法。

    2026-09-25 现场事故：规则写了「受理通知不许暗示社员改文档」，
    而**同一个文件里** accepted 的例子仍写着「如果其实是晚上，把文档改一下告诉我就行」。
    规则改了、例子没改，LLM 上线的行为是照**例子**走的。

    这条守卫就是为了堵这一类自相矛盾：规矩块以外的地方再出现这些说法就红。
    （`kb/guide.md` 不在此列——它正经地教社员改草稿，语境不同。）
    """
    text = PromptLoader().load("polling")
    rules = _prompt_copy_rules_block(text)
    assert "不许出现任何「改文档」的暗示" in rules, (
        "文案规矩块里少了「不许暗示社员改文档」这条——它是拿现场事故换来的，别删"
    )

    rest = text.replace(rules, "")
    offenders = [
        f"第 {no} 行（{phrase!r}）：{line.strip()}"
        for no, line in enumerate(rest.splitlines(), start=1)
        for phrase in TELL_MEMBER_TO_EDIT_PHRASES
        if phrase in line
    ]
    assert not offenders, (
        "提示词里的例子又让社员去改文档了（受理后文档已锁定，他一改就收到「改动无效」）：\n  "
        + "\n  ".join(offenders)
        + "\n如果确实是必要的，先想清楚语境（只有 `rejected` 的文档没锁定），"
        "再改这条守卫与规矩块。"
    )
