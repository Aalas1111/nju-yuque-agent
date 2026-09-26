"""`yqa reset-test-data`：测试完回到干净起点。

这条命令会**真的删知识库里的文档**，所以规矩比别的命令严：

* **默认只预览**，不加 `--yes` 一行都不删；
* 《指导文档（必读）》《Agent 通知》**永不删**；
* 删掉的文档要从状态快照里同步摘掉——否则下次轮询会把它当成「被删除」而发通知；
* `runs/` 默认保留（那是唯一事实来源）。

这几个性质都在下面锁住。全程离线：`cli.YuqueClient` 被换成假件。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fakes import FakeYuque, make_meta, make_toc
from yuque_agent import cli, clock
from yuque_agent.config import Settings
from yuque_agent.week import cycle_targets

#: 夹具里的文档必须落在**当前**周期目录里——`--scope cycle` 的语义就是「清掉当前周期」。
#: 写死周期名会让测试在周期翻转那天自己坏掉（2026-09-26 真的坏过一次）。
CYCLE, PREV_CYCLE = (week.title for week in cycle_targets(clock.now()))


def build_client() -> FakeYuque:
    """一个典型的「测试完」的知识库：周期目录里有 2 篇申请，归档区 1 篇，外加 2 篇系统文档。"""
    toc = make_toc(
        ("指导文档（必读）", "DOC", 100, ""),
        ("Agent 通知", "DOC", 101, ""),
        (CYCLE, "TITLE", 0, ""),
        ("测试甲", "DOC", 1, CYCLE),
        ("测试乙", "DOC", 2, CYCLE),
        ("归档区", "TITLE", 0, ""),
        (PREV_CYCLE, "TITLE", 0, "归档区"),
        ("老申请", "DOC", 3, PREV_CYCLE),
    )
    return FakeYuque(
        toc_nodes=toc,
        doc_metas=[
            make_meta(100, "指导文档（必读）"),
            make_meta(101, "Agent 通知"),
            make_meta(1, "测试甲"),
            make_meta(2, "测试乙"),
            make_meta(3, "老申请"),
        ],
    )


@pytest.fixture()
def env(tmp_path, monkeypatch) -> dict[str, Any]:
    workspace = tmp_path / "ws"
    settings = Settings(repo="g/kb", workspace=workspace)
    settings.ensure_dirs()

    client = build_client()
    monkeypatch.setattr(cli, "YuqueClient", lambda **kwargs: client)

    # 造一些本地产物
    for name in ("2026-09-24-1.json", "2026-09-24-2.json"):
        (settings.applications_dir / name).write_text("{}", encoding="utf-8")
    (settings.notify_dir / "pending").mkdir(parents=True, exist_ok=True)
    (settings.notify_dir / "pending" / "000001-accepted-aaaa.json").write_text(
        "{}", encoding="utf-8"
    )
    (settings.notify_dir / ".seq").write_text("7", encoding="utf-8")
    (settings.notify_dir / "outbox.jsonl").write_text('{"seq": 1}\n', encoding="utf-8")
    (settings.notes_dir / "accepted.json").write_text("{}", encoding="utf-8")

    # 状态快照里有那两篇测试文档（字段要齐，否则真加载器解析不了）
    def snap(doc_id: int, title: str, where: str = "") -> dict:
        return {
            "doc_id": doc_id,
            "slug": f"s{doc_id}",
            "title": title,
            "updated_at": "2026-09-20T00:00:00Z",
            "created_at": "2026-09-19T00:00:00Z",
            "author": "模拟",
            "dir": where,
            "content_sha256": "",
        }

    state = {
        "version": 1,
        "rounds": 3,
        "last_archive_title": CYCLE,
        "snapshot": {
            "docs": {
                "100": snap(100, "指导文档（必读）"),
                "1": snap(1, "测试甲", CYCLE),
                "2": snap(2, "测试乙", CYCLE),
            }
        },
    }
    settings.state_file.write_text(json.dumps(state), encoding="utf-8")

    return {"workspace": workspace, "settings": settings, "client": client}


def run_reset(env: dict[str, Any], *args: str):
    return CliRunner().invoke(
        cli.app,
        [
            "reset-test-data",
            "--repo",
            "g/kb",
            "--workspace",
            str(env["workspace"]),
            *args,
        ],
    )


def deleted_ids(env: dict[str, Any]) -> list[int]:
    return [c[1]["doc_id"] for c in env["client"].calls if c[0] == "delete_doc"]


# ---------------------------------------------------------------- 工作区守卫


def test_refuses_a_workspace_inside_the_code_checkout(env, tmp_path, monkeypatch) -> None:
    """`--workspace` 指到代码检出里 → 直接拒绝。

    这是实测踩出来的坑（2026-09-25）：`yqa-as-service` 没带 `--workspace`，
    默认落到 `<检出>/workspace`——语雀里的申请文档照样被删，本地产出却清了个空。
    """
    monkeypatch.setattr(cli, "CHECKOUT_ROOT", tmp_path)
    inside = tmp_path / "checkout" / "workspace"
    result = CliRunner().invoke(
        cli.app,
        ["reset-test-data", "--repo", "g/kb", "--workspace", str(inside), "--yes"],
    )
    assert result.exit_code == 2
    assert deleted_ids(env) == [], "守卫拦下之后不该再删任何语雀文档"


def test_force_overrides_the_checkout_guard(env, tmp_path, monkeypatch) -> None:
    """知道自己在干什么时用 --force 放行。"""
    monkeypatch.setattr(cli, "CHECKOUT_ROOT", tmp_path)
    inside = tmp_path / "checkout" / "workspace"
    Settings(repo="g/kb", workspace=inside).ensure_dirs()
    result = CliRunner().invoke(
        cli.app,
        [
            "reset-test-data",
            "--repo",
            "g/kb",
            "--workspace",
            str(inside),
            "--yes",
            "--force",
        ],
    )
    assert result.exit_code == 0
    assert deleted_ids(env) != []


# ---------------------------------------------------------------- 预览是默认


def test_dry_run_deletes_nothing(env: dict[str, Any]) -> None:
    result = run_reset(env)

    assert result.exit_code == 0
    assert deleted_ids(env) == [], "不加 --yes 一行都不该删"
    assert (env["settings"].applications_dir / "2026-09-24-1.json").exists()
    assert "没有 --yes" in result.output or "预览" in result.output


def test_dry_run_lists_what_would_be_deleted(env: dict[str, Any]) -> None:
    out = run_reset(env).output
    assert "测试甲" in out and "测试乙" in out
    assert "老申请" not in out, "默认 scope=cycle，不该动归档区"


# ---------------------------------------------------------------- --yes 真删


def test_yes_deletes_cycle_docs_and_spares_system_docs(env: dict[str, Any]) -> None:
    result = run_reset(env, "--yes")

    assert result.exit_code == 0
    assert sorted(deleted_ids(env)) == [1, 2]
    assert 100 not in deleted_ids(env) and 101 not in deleted_ids(env), "系统文档永不删"
    assert 3 not in deleted_ids(env), "scope=cycle 不该动归档区"


def test_scope_all_also_clears_the_archive(env: dict[str, Any]) -> None:
    run_reset(env, "--yes", "--scope", "all")
    assert sorted(deleted_ids(env)) == [1, 2, 3]
    assert 100 not in deleted_ids(env) and 101 not in deleted_ids(env)


def test_bad_scope_is_rejected_without_touching_anything(env: dict[str, Any]) -> None:
    result = run_reset(env, "--yes", "--scope", "everything")
    assert result.exit_code == 2
    assert deleted_ids(env) == []


# ---------------------------------------------------------------- 本地清理


def test_yes_clears_local_artifacts(env: dict[str, Any]) -> None:
    settings = env["settings"]
    run_reset(env, "--yes")

    assert list(settings.applications_dir.glob("*.json")) == [
        settings.applications_dir / "index.json"
    ], "申请清空，只留重建出来的空索引"
    assert list((settings.notify_dir / "pending").glob("*.json")) == []
    assert (settings.notify_dir / ".seq").read_text() == "0"
    assert (settings.notify_dir / "outbox.jsonl").read_text() == ""
    assert list(settings.notes_dir.iterdir()) == []


def test_notes_of_any_extension_are_cleared(env: dict[str, Any]) -> None:
    """**回归**：notes/ 是 LLM 的跨轮记忆，格式由它自己定。

    实测它写过 ``notes/accepted_docs.md``（markdown，不是 json）。
    重置时如果只删 ``*.json``，就会把「哪些文档已受理过」的记忆留着——
    下次跑的时候 agent 会以为那些申请还在。
    """
    (env["settings"].notes_dir / "accepted_docs.md").write_text("# 已受理\n", encoding="utf-8")

    run_reset(env, "--yes")

    assert list(env["settings"].notes_dir.iterdir()) == [], "notes/ 里啥都不该剩"


def test_runs_are_kept_by_default(env: dict[str, Any]) -> None:
    """留痕是唯一事实来源，默认不能删。"""
    runs = env["settings"].runs_dir / "20260921-000000-polling-aaaa"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "session.jsonl").write_text("{}\n", encoding="utf-8")

    run_reset(env, "--yes")
    assert runs.exists(), "默认不该删 runs/"

    run_reset(env, "--yes", "--runs")
    assert not runs.exists(), "加了 --runs 才删"


# ---------------------------------------------------------------- 状态同步


def test_deleted_docs_are_pruned_from_the_snapshot(env: dict[str, Any]) -> None:
    """**这条最关键**：不摘掉快照，下次轮询会当成「文档被删除」而发通知。"""
    run_reset(env, "--yes")

    state = json.loads(env["settings"].state_file.read_text(encoding="utf-8"))
    docs = state["snapshot"]["docs"]
    assert "1" not in docs and "2" not in docs
    assert "100" in docs, "没被删的文档要留在快照里"


def test_state_file_is_not_deleted(env: dict[str, Any]) -> None:
    """不能删 state.json——那会被当成冷启动，下次启动白跑一轮归档（约 2 万 token）。"""
    run_reset(env, "--yes")
    assert env["settings"].state_file.exists()
    state = json.loads(env["settings"].state_file.read_text(encoding="utf-8"))
    assert state["last_archive_title"] == CYCLE, "归档水位线要保住"


# ---------------------------------------------------------------- Agent 通知


def test_notice_doc_is_never_deleted(env: dict[str, Any]) -> None:
    run_reset(env, "--yes", "--scope", "all")
    assert 101 not in deleted_ids(env), "《Agent 通知》是系统性文档，永远不删"


def test_notice_doc_is_rebuilt_empty_after_local_wipes(env: dict[str, Any]) -> None:
    """本地通知清空了，《Agent 通知》就该只剩「本周期还没有通知」——它是重建出来的。"""
    run_reset(env, "--yes")
    bodies = [c[1]["body"] for c in env["client"].calls if c[0] == "update_doc"]
    assert len(bodies) == 1, f"应当恰好重写一次通知文档，实际 {len(bodies)} 次"
    assert "本周期还没有通知" in bodies[0]
