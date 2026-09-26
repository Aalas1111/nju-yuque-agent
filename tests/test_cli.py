"""CLI 层测试（`yqa once` 这一层）。

**为什么要单独有这一层**：`poll_once` 被 `test_debounce.py` 测得很细，但那只保证
**runner** 的行为。`cli.py` 把命令接到 runner 上的那一行**接线**以前没有任何测试守着。

实测就在真机上踩了这个坑：社员刚写完文档，敲 `yqa once`，程序说「知识库没有变化」——
其实变化被静默期吃掉了（`once` 没传 `debounce=False`）。而
`test_debounce.py::test_force_bypasses_nothing_but_still_needs_quiet` 的文档字符串里
甚至写着「手动命令请用 once（它关掉合并）」——**意图写下来了，实现没跟上，测试也没守住**。

所以这一组的重点是「命令 → runner 的接线」，而不是重新测一遍 runner。
全程离线：`_clients` 被换成假件，绝不碰真语雀。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fakes import FakeLLM, FakeYuque, call, make_meta, make_toc
from yuque_agent import cli
from yuque_agent.config import Settings
from yuque_agent.llm import LLMResponse


def done(verdict: str = "accepted", summary: str = "看过了") -> LLMResponse:
    return call("done", verdict=verdict, summary=summary)


@pytest.fixture()
def wired(tmp_path, monkeypatch) -> dict[str, Any]:
    """把 `cli._clients` 换成假件，并把假件与 settings 一起交出来。

    假件要**在这里就建好**（而不是等 `_clients` 被调用时再建）：测试需要先往
    `client` 里摆文档，再敲命令。

    `once` 里 `client.close()` / `llm.close()` 在 `finally` 里调用，所以假件也必须
    有 `close()`——没有的话测试会因为一个与断言无关的 `AttributeError` 变红。
    """
    box: dict[str, Any] = {"workspace": tmp_path / "ws"}
    client = FakeYuque()
    llm = FakeLLM(script=[done()] * 6)
    box["client"] = client
    box["llm"] = llm

    def fake_clients(settings: Settings):
        box["settings"] = settings
        return client, llm

    monkeypatch.setattr(cli, "_clients", fake_clients)
    return box


def set_docs(client: FakeYuque, *docs: tuple[int, str], updated: str = "t1") -> None:
    """把知识库摆成「0919-0925 周期目录下有这几篇文档」。"""
    client.doc_metas = [make_meta(doc_id, title, updated_at=updated) for doc_id, title in docs]
    client.toc_nodes = make_toc(
        ("0919-0925", "TITLE", 0, ""),
        *[(title, "DOC", doc_id, "0919-0925") for doc_id, title in docs],
    )
    for doc_id, title in docs:
        client.bodies[doc_id] = f"申请人：{title}"


def yqa_once(wired: dict[str, Any], *args: str):
    return CliRunner().invoke(
        cli.app,
        ["once", "--repo", "g/kb", "--workspace", str(wired["workspace"]), *args],
    )


# ------------------------------------------------- 核心回归：once 不该等静默期


def test_once_wakes_immediately_after_a_write(wired) -> None:
    """**这条就是那个 bug 的回归测试。**

    写完文档立刻 `yqa once`，必须当场唤醒 LLM；不能因为「距上次变更不满 quiet_seconds」
    就默默跳过——人工命令不是噪声，没有合并的必要。
    """
    client = wired["client"]
    llm = wired["llm"]

    set_docs(client)  # 空知识库，先建基线
    assert yqa_once(wired).exit_code == 0
    assert llm.calls == 0, "冷启动只建基线，不该唤醒 LLM"

    set_docs(client, (1, "新生见面会"))  # 社员刚写完
    result = yqa_once(wired)

    assert result.exit_code == 0
    assert llm.calls == 1, "写完文档立刻 once，必须马上唤醒 LLM（曾经被静默期吃掉）"
    assert "没有变化" not in result.output, f"不该说「没有变化」，实际输出：{result.output!r}"


def test_once_does_not_say_no_change_while_in_quiet_period(wired) -> None:
    """就算真被静默期拦住，也不能说「知识库没有变化」——那是假话。

    这里直接把 runner 摆成「静默期跳过」，再看 `once` 打印哪一句。
    （正常路径下 `once` 不会走到这里，但提示语必须**构造上**是真的。）
    """
    from yuque_agent.runner import Runner

    settings = Settings(repo="g/kb", workspace=wired["workspace"], quiet_seconds=45)
    settings.ensure_dirs()
    runner = Runner(settings=settings, client=wired["client"], llm=wired["llm"])

    runner.last_skip = "no_change"
    assert "没有变化" in cli._poll_skip_message(runner, settings)

    runner.last_skip = "quiet_period"
    quiet_text = cli._poll_skip_message(runner, settings)
    assert "没有变化" not in quiet_text, "静默期里不能说「没有变化」"
    assert "静默期" in quiet_text and "45" in quiet_text


# ------------------------------------------------- 其余人工入口


def test_once_reports_no_change_when_kb_is_quiet(wired) -> None:
    """真的没变化时，还是那句「没有变化」——别把好的也改了。"""
    set_docs(wired["client"])
    yqa_once(wired)  # 建基线
    result = yqa_once(wired)

    assert "没有变化" in result.output
    assert wired["llm"].calls == 0


def test_force_wakes_even_without_any_change(wired) -> None:
    """`--force` = 「无视 diff，强制唤醒 LLM」，人工敲了就该跑。"""
    set_docs(wired["client"])
    yqa_once(wired)  # 建基线

    result = yqa_once(wired, "--force")

    assert result.exit_code == 0
    assert wired["llm"].calls == 1, "--force 应当无视 diff 直接唤醒"


def test_rescan_wakes_and_is_documented_as_rescan(wired) -> None:
    """`--rescan` 是既有的绕过路径，别在改动中被碰坏。

    注意：空知识库上 rescan 会什么都不做（没有文档可重扫）——那是对的，
    所以这里要先摆一篇文档进去。
    """
    set_docs(wired["client"], (1, "新生见面会"))

    result = yqa_once(wired, "--rescan")

    assert result.exit_code == 0
    assert wired["llm"].calls == 1
    assert "rescan" in (wired["llm"].seen_messages[-1][-1]["content"]), "rescan 指令要传给 LLM"


def test_once_passes_debounce_off_to_the_runner(tmp_path, monkeypatch) -> None:
    """直接盯住那行接线：`once` 必须把 `debounce=False` 传下去。

    前面几条是**行为**断言，这条是**接线**断言。行为测试依赖 fakes 的语义，
    接线测试直接看参数——两者都要，因为出问题的就是参数没传。
    """
    seen: dict[str, Any] = {}

    class SpyRunner:
        def __init__(self, *, settings, client, llm):  # noqa: ANN001, ARG002
            self.last_skip = ""

        def poll_once(self, **kwargs):  # noqa: ANN003
            seen.update(kwargs)
            return None

    monkeypatch.setattr(cli, "Runner", SpyRunner)
    monkeypatch.setattr(cli, "_clients", lambda settings: (FakeYuque(), FakeLLM()))

    yqa_once({"workspace": tmp_path / "ws"})

    assert seen.get("debounce") is False, f"once 必须传 debounce=False，实际收到 {seen!r}"


# ------------------------------------------------- 常驻路径不该被顺手改坏


def test_resident_path_still_debounces(wired, tmp_path) -> None:
    """`yqa run` 的静默期合并必须原样保留——那是「不要为噪声付钱」的核心。

    实测：不合并的话，语雀手工建一篇文档会分几步产生变更，一篇就要唤醒十几次 LLM。
    同时锁住 `last_skip`：被静默期拦住时要照实说，不能报成「没变化」。
    """
    from datetime import timedelta

    from tests.test_debounce import dt
    from yuque_agent.runner import Runner

    client, llm = wired["client"], wired["llm"]
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws2", quiet_seconds=45)
    settings.ensure_dirs()
    runner = Runner(settings=settings, client=client, llm=llm)

    set_docs(client)
    assert runner.poll_once(now=dt("2026-09-20T10:00:00")) is None  # 基线
    assert runner.last_skip == "no_change"

    set_docs(client, (1, "新生见面会"))
    t0 = dt("2026-09-20T10:01:00")
    assert runner.poll_once(now=t0) is None, "常驻路径下刚变时不该唤醒"
    assert runner.last_skip == "quiet_period", "要如实报告「被静默期拦住」"
    assert llm.calls == 0

    assert runner.poll_once(now=t0 + timedelta(seconds=46)) is not None, "静默期结束后应当唤醒"
    assert llm.calls == 1
    assert runner.last_skip == "", "真跑了就不该留跳过原因"


# ---------------------------------------------------------------- sync-guide


def test_sync_guide_fills_the_notice_url() -> None:
    """`guide.md` 里的 `{{notice_url}}` 必须在上传前换成《Agent 通知》**当前**的地址。

    为什么要有这一步：那篇文档的 URL 带着语雀生成的 slug，文档一旦被删掉重建就会变，
    写死的链接会**静默失效**（需求方 2026-09-26 审定的《指导文档》里就有这条链接）。
    """
    settings = Settings(repo="g/kb", workspace=Path("ws"))
    body = "看 [Agent通知]({{notice_url}}) 。"

    client = FakeYuque(doc_metas=[make_meta(7, "Agent 通知")])
    assert cli._fill_notice_link(client, settings, body) == (  # type: ignore[arg-type]
        "看 [Agent通知](https://www.yuque.com/g/kb/s7) 。"
    )

    # 那篇文档还没建时：整条链接降级成纯文本，别留一个指向 `{{notice_url}}` 的坏链接
    assert cli._fill_notice_link(FakeYuque(), settings, body) == "看 Agent通知 。"  # type: ignore[arg-type]


def test_guide_source_has_no_hardcoded_kb_url() -> None:
    """源文件里不许写死知识库地址：`Agent 通知` 的 slug 会变（见上一条）。"""
    import yuque_agent

    guide = (Path(yuque_agent.__file__).parent / "kb" / "guide.md").read_text(encoding="utf-8")
    assert "{{notice_url}}" in guide, "链接占位符不见了？"
    assert "yuque.com/" not in guide, "别把知识库 URL 写死（slug 变了就静默失效）"


# ------------------------------------------------- 没有默认知识库（2026-09-27）


def test_repo_is_required_and_the_error_says_what_to_do(monkeypatch) -> None:
    """`--repo` 与 `YQA_REPO` 都没有 → 当场停下（exit 2）并说清怎么配。

    为什么钉死：2026-09-27 正式迁移时删掉了写死的默认 namespace。
    留着它的失败模式很隐蔽——**忘配的人会安安静静连到别人的知识库**；
    而当场报错只烦这一次。
    """
    monkeypatch.delenv("YQA_REPO", raising=False)
    result = CliRunner().invoke(cli.app, ["once", "--workspace", "ws"])
    assert result.exit_code == 2, result.output
    assert "YQA_REPO" in result.output
    assert "默认知识库" in result.output


def test_repo_falls_back_to_the_env_var(wired, monkeypatch) -> None:
    """不写 `--repo` 时从 `YQA_REPO` 读——两条路有一条通就不该拦。"""
    monkeypatch.setenv("YQA_REPO", "g/kb")
    set_docs(wired["client"])

    result = CliRunner().invoke(cli.app, ["once", "--workspace", str(wired["workspace"])])

    assert result.exit_code == 0, result.output
    assert wired["settings"].repo == "g/kb"
