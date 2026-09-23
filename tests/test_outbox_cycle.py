"""产物的**周期边界**：活跃 / 归档分离，以及交付件 plan.json 的自动维护。

为什么这一组测试重要（真机上发现的问题）：

原来 ``outbox/applications/`` **只增不减**，而 ``build_plan_json()`` 扫的是整个目录、
没有任何周期过滤。于是周期翻转之后，上几周的申请仍然留在 ``plan.json`` 里，
cac 照着提交就是**订一个已经过去的日期**。当时看不出来，只因为知识库刚清空过。

而且 ``plan.json`` 在真机上**从来没生成过**——它只由人工 ``yqa export-plan -o`` 产生，
常驻服务里没有任何地方调它。下游拿不到推送，只能靠「文件是新的」。

所以这里钉四件事：
1. 申请带上 ``cycle``，plan.json 也带；
2. 周期翻转时**程序**把活跃产物搬进 ``archive/<周期>/``（不依赖 LLM 的归档会话）；
3. 每次写申请就重发 ``plan.json``，且**借用人信息不能丢**；
4. 搬移是幂等的，重复搬不会丢文件。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from yuque_agent import outputs
from yuque_agent.config import Settings
from yuque_agent.runner import Runner, State
from yuque_agent.week import cycle_of

from .fakes import FakeLLM, FakeYuque


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings(repo="g/kb", workspace=tmp_path / "ws")
    s.ensure_dirs()
    return s


def application_payload(doc_id: int = 11, date: str = "2026-09-23") -> dict:
    return {
        "source": {"repo": "g/kb", "doc_id": doc_id, "title": "新生见面会"},
        "activity": {
            "title": "新生见面会",
            "date": date,
            "period": "7-8",
            "people": 25,
            "campus": "3",
            "building": "12",
            "room_type": None,
            "preferred_room": None,
        },
        "raw": {"text": "想借教室"},
        "derived": {},
        "agent": {},
    }


def _runner(settings: Settings) -> Runner:
    return Runner(settings=settings, client=FakeYuque(), llm=FakeLLM())


# -- 1. 周期标记 -----------------------------------------------------------


def test_application_carries_the_cycle(settings: Settings) -> None:
    result = outputs.write_application(settings, application_payload())
    data = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert data["cycle"] == outputs.current_cycle(settings)


def test_plan_carries_the_cycle_and_a_timestamp(settings: Settings) -> None:
    outputs.write_application(settings, application_payload())
    plan = json.loads(settings.plan_file.read_text(encoding="utf-8"))
    assert plan["cycle"] == outputs.current_cycle(settings)
    assert plan["generated_at"]
    assert len(plan["activities"]) == 1


# -- 2. 每次写申请就重发 plan.json ----------------------------------------


def test_writing_an_application_publishes_the_plan(settings: Settings) -> None:
    """下游拿不到推送，只能靠这个文件是新的——所以写申请必须顺手重发。"""
    assert not settings.plan_file.exists()
    outputs.write_application(settings, application_payload())
    assert settings.plan_file.exists(), "写申请没有刷新 outbox/plan.json"


def test_plan_only_contains_the_active_cycle(settings: Settings) -> None:
    outputs.write_application(settings, application_payload(doc_id=1))
    outputs.write_application(settings, application_payload(doc_id=2, date="2026-09-24"))
    plan = json.loads(settings.plan_file.read_text(encoding="utf-8"))
    assert {a["_doc_id"] for a in plan["activities"]} == {1, 2}

    # 归档掉之后，活跃 plan 里不该再有它们（这是「订过去的日期」那个 bug 的核心）
    outputs.rotate_outbox(settings, cycle=plan["cycle"])
    outputs.publish_plan(settings)
    plan2 = json.loads(settings.plan_file.read_text(encoding="utf-8"))
    assert plan2["activities"] == []


def test_borrower_defaults_survive_auto_republish(settings: Settings) -> None:
    """借用人信息必须落盘——否则 agent 每次自动重发都把它丢了。"""
    outputs.write_plan_defaults(settings, {"JYRXM": "张三", "JSJYLXDM": "02"})
    outputs.publish_plan(settings)
    outputs.write_application(settings, application_payload())
    plan = json.loads(settings.plan_file.read_text(encoding="utf-8"))
    assert plan["defaults"] == {"JYRXM": "张三", "JSJYLXDM": "02"}


# -- 3. 归档搬移 -----------------------------------------------------------


def test_rotate_moves_everything_into_the_cycle_folder(settings: Settings) -> None:
    outputs.write_application(settings, application_payload(doc_id=1))
    outputs.write_application(settings, application_payload(doc_id=2, date="2026-09-24"))
    cycle = outputs.current_cycle(settings)

    outcome = outputs.rotate_outbox(settings, cycle=cycle)
    assert outcome["cycle"] == cycle

    dest = settings.cycle_archive_dir(cycle)
    assert (dest / "plan.json").is_file(), "归档里没有当周那份 plan.json（交付物没留底）"
    assert (dest / "applications" / "index.json").is_file()
    archived = sorted(p.name for p in (dest / "applications").glob("*.json"))
    assert len(archived) == 3  # 两份申请 + index.json

    # 活跃侧应该只剩一个重建出来的空索引，而且目录还在
    # （下游与 reset 都指望它在；索引里 count=0 才对）
    assert [p.name for p in settings.applications_dir.glob("*.json")] == ["index.json"]
    index = json.loads((settings.applications_dir / "index.json").read_text(encoding="utf-8"))
    assert index["count"] == 0
    assert settings.applications_dir.is_dir()
    assert not settings.plan_file.exists()


def test_rotate_keeps_the_archived_plan_frozen(settings: Settings) -> None:
    """归档的 plan.json 是「那周到底交付了什么」的凭证，不能被后来的重发覆盖。"""
    outputs.write_application(settings, application_payload(doc_id=1))
    cycle = outputs.current_cycle(settings)
    outputs.rotate_outbox(settings, cycle=cycle)
    frozen = json.loads(
        (settings.cycle_archive_dir(cycle) / "plan.json").read_text(encoding="utf-8")
    )

    outputs.write_application(settings, application_payload(doc_id=99, date="2026-10-01"))
    after = json.loads(
        (settings.cycle_archive_dir(cycle) / "plan.json").read_text(encoding="utf-8")
    )
    assert frozen == after


def test_rotate_is_idempotent(settings: Settings) -> None:
    """重复搬同一个周期不能把文件冲掉（宁可多留，也别丢）。"""
    outputs.write_application(settings, application_payload(doc_id=1))
    cycle = outputs.current_cycle(settings)
    outputs.rotate_outbox(settings, cycle=cycle)

    outputs.write_application(settings, application_payload(doc_id=2, date="2026-09-24"))
    outputs.rotate_outbox(settings, cycle=cycle)

    names = sorted(p.name for p in (settings.cycle_archive_dir(cycle) / "applications").glob("*"))
    # 两份申请都在（第二份因为重名被加了时间戳后缀）
    assert sum(1 for n in names if n.endswith(".json") and "index" not in n) == 2


def test_rotate_on_an_empty_outbox_does_not_create_junk(settings: Settings) -> None:
    for path in settings.applications_dir.glob("*.json"):
        path.unlink()
    output = outputs.detect_active_cycle(settings)
    assert output == ""
    settings.plan_file.write_text("{}", encoding="utf-8")
    outputs.rotate_outbox(settings, cycle="0919-0925")
    # plan.json 被搬走了，活跃侧不留残骸
    assert not settings.plan_file.exists()


# -- 4. 周期翻转由程序做 ---------------------------------------------------


def _moment_in(cycle_start: datetime) -> datetime:
    return cycle_start


def test_runner_rotates_when_the_cycle_flips(settings: Settings) -> None:
    """核心：翻转是**程序**的事，不看 LLM 的归档会话成没成。"""
    runner = _runner(settings)
    old = datetime(2026, 9, 20, 10, 0)  # 周期 0919-0925 之内
    assert runner.rotate_cycle_if_needed(old) is None  # 第一次只记基线
    assert runner.state.active_cycle == "0919-0925"

    outputs.write_application(settings, application_payload())
    assert settings.plan_file.exists()

    # 跨到下一个周期（周六 00:00）
    new = old + timedelta(days=7)
    outcome = runner.rotate_cycle_if_needed(new)
    assert outcome is not None and outcome["cycle"] == "0919-0925"
    assert runner.state.active_cycle == "0926-1002"
    assert (settings.cycle_archive_dir("0919-0925") / "plan.json").is_file()
    assert not settings.plan_file.exists()


def test_runner_does_not_rotate_within_the_same_cycle(settings: Settings) -> None:
    runner = _runner(settings)
    start = datetime(2026, 9, 19, 0, 0)
    runner.rotate_cycle_if_needed(start)
    outputs.write_application(settings, application_payload())
    for days in (1, 3, 6):
        assert runner.rotate_cycle_if_needed(start + timedelta(days=days)) is None
    assert settings.plan_file.exists(), "同一周期内不该把活跃产物搬走"


def test_rotation_does_not_need_the_llm(settings: Settings) -> None:
    """翻转不该花一个 token——LLM 一次都不许被调用。"""

    class _Explode:
        def chat(self, *a: object, **k: object) -> None:
            raise AssertionError("周期翻转不该叫 LLM")

        def close(self) -> None:
            pass

    runner = Runner(settings=settings, client=FakeYuque(), llm=_Explode())
    outputs.write_application(settings, application_payload())
    runner.rotate_cycle_if_needed(datetime(2026, 9, 20))
    runner.rotate_cycle_if_needed(datetime(2026, 9, 27))


def test_upgrade_path_infers_the_cycle_from_existing_files(settings: Settings) -> None:
    """state 里没记过（老部署）时，从申请自带的 cycle 推断，别当成当前周期。"""
    outputs.write_application(settings, application_payload())
    old_cycle = outputs.current_cycle(settings)

    # 模拟升级：state 丢了 active_cycle 的记录
    runner = _runner(settings)
    runner.state.active_cycle = ""
    runner.state.snapshot = None

    future = datetime(2026, 10, 4)  # 已经翻了好几个周期
    outcome = runner.rotate_cycle_if_needed(future)
    assert outcome is not None
    assert outcome["cycle"] == old_cycle, "老文件被当成当前周期了，永远不会归档"
    assert runner.state.active_cycle == cycle_of(future.date()).title


# -- 5. 给管理员的 plan_updated 提醒 --------------------------------------


def test_plan_notice_fires_once_per_distinct_plan(settings: Settings) -> None:
    runner = _runner(settings)
    outputs.write_application(settings, application_payload(doc_id=1))
    first = runner.notify_plan_updated_if_changed()
    assert first is not None and first["kind"] == "plan_updated"

    # 清单没变 → 不重发（否则每轮都打扰管理员）
    assert runner.notify_plan_updated_if_changed() is None

    # 清单真变了 → 再发一条，而且只有一条（不是一份申请一条）
    outputs.write_application(settings, application_payload(doc_id=2, date="2026-09-24"))
    second = runner.notify_plan_updated_if_changed()
    assert second is not None
    assert runner.notify_plan_updated_if_changed() is None
    pending = sorted(settings.notify_dir.glob("pending/*plan_updated*.json"))
    assert len(pending) == 2


def test_plan_notice_is_silent_for_an_empty_plan(settings: Settings) -> None:
    """周期刚翻转时清单是空的——不该发一条「请下载」去打扰人。"""
    runner = _runner(settings)
    assert runner.notify_plan_updated_if_changed() is None
    assert list(settings.notify_dir.glob("pending/*plan_updated*.json")) == []


def test_plan_notice_says_download_not_done(settings: Settings) -> None:
    """文案必须是「请下载」，**不能**声称已办结——我们无从知道 cac 交没交。"""
    runner = _runner(settings)
    outputs.write_application(settings, application_payload())
    result = runner.notify_plan_updated_if_changed()
    assert result is not None
    text = Path(result["path"]).read_text(encoding="utf-8")
    assert "下载" in text
    for bad in ("已处理", "已办结", "已提交"):
        assert bad not in text, f"通知里出现了「{bad}」——那是我们不知道的事"


def test_plan_notice_records_the_fingerprint(settings: Settings) -> None:
    runner = _runner(settings)
    outputs.write_application(settings, application_payload())
    runner.notify_plan_updated_if_changed()
    assert runner.state.plan_notified  # 指纹进了 state，重启后不会重发
    reloaded = State.from_json(runner.state.to_json())
    assert reloaded.plan_notified == runner.state.plan_notified
