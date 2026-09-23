"""对外契约：申请 JSON 与通知事件。

这两个格式是交给另外两位同学的接口，所以「格式稳定」比「功能多」重要。
"""

from __future__ import annotations

import json
import sys

import pytest

from yuque_agent.config import Settings
from yuque_agent.outputs import (
    CRB_ACTIVITY_FIELDS,
    ContractError,
    build_plan_json,
    make_application_id,
    write_application,
    write_notice,
)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings(repo="g/kb", workspace=tmp_path / "ws")
    s.ensure_dirs()
    return s


def application_payload(doc_id: int = 11, date: str = "2026-09-23") -> dict:
    """模拟 :func:`tools._emit_application` 组装好的结构（``activity`` 已对齐 crb）。"""
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
            "preferred_room": "仙I-201",
        },
        "raw": {"campus": "仙林", "building": "仙II区", "start": "16:10", "end": "18:00"},
        "derived": {"campus_name": "仙林", "ksjc": 7, "jsjc": 8},
        "agent": {"run_id": "r1", "verdict": "accepted"},
    }


# ---------------------------------------------------------------- 申请


def test_application_id_is_stable_and_readable() -> None:
    assert make_application_id("2026-09-23", 11) == "2026-09-23-11"
    assert make_application_id("2026-09-23", 11) == make_application_id("2026-09-23", 11)


def test_write_application_is_idempotent(settings: Settings) -> None:
    first = write_application(settings, application_payload())
    second = write_application(settings, application_payload())
    assert first["application_id"] == second["application_id"]
    assert first["replaced_existing"] is False
    assert second["replaced_existing"] is True
    files = [p for p in settings.applications_dir.glob("*.json") if p.name != "index.json"]
    assert len(files) == 1, "同一篇文档重复受理不该产生第二个文件"


def test_application_index_is_rebuilt_from_disk(settings: Settings) -> None:
    write_application(settings, application_payload(doc_id=11))
    write_application(settings, application_payload(doc_id=12, date="2026-09-24"))
    index = json.loads((settings.applications_dir / "index.json").read_text(encoding="utf-8"))
    assert index["count"] == 2
    assert [row["doc_id"] for row in index["applications"]] == [11, 12]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("activity"),
        lambda p: p.pop("source"),
        lambda p: p["source"].pop("doc_id"),
        lambda p: p["activity"].pop("date"),
        lambda p: p["activity"].pop("period"),
        lambda p: p["activity"].pop("campus"),
    ],
)
def test_write_application_validates_essentials(settings: Settings, mutate) -> None:
    payload = application_payload()
    mutate(payload)
    with pytest.raises(ContractError):
        write_application(settings, payload)


def test_activity_is_trimmed_to_the_crb_contract(settings: Settings) -> None:
    """写盘时只保留下游真正会读的字段，防止内部字段渗进契约。"""
    payload = application_payload()
    payload["activity"]["internal_debug"] = "不应出现"
    write_application(settings, payload)
    record = json.loads(
        (settings.applications_dir / "2026-09-23-11.json").read_text(encoding="utf-8")
    )
    assert set(record["activity"]) == set(CRB_ACTIVITY_FIELDS)
    assert record["raw"] and record["derived"] and record["agent"]


def test_export_plan_is_crb_ready(settings: Settings) -> None:
    """export-plan 的输出应该能直接当 crb 的 plan 文件用。"""
    write_application(settings, application_payload(doc_id=11))
    write_application(settings, application_payload(doc_id=12, date="2026-09-24"))
    plan = build_plan_json(settings, defaults={"JSJYLXDM": "02"})
    assert plan["defaults"] == {"JSJYLXDM": "02"}
    assert len(plan["activities"]) == 2
    first = plan["activities"][0]
    for key in CRB_ACTIVITY_FIELDS:
        assert key in first
    assert first["campus"] == "3" and first["period"] == "7-8"
    assert first["_doc_id"] == 11


def test_dry_run_writes_nothing(settings: Settings) -> None:
    settings.dry_run = True
    write_application(settings, application_payload())
    assert list(settings.applications_dir.glob("*.json")) == []


# ---------------------------------------------------------------- 通知


def notice_kwargs(**overrides) -> dict:
    base = {"kind": "rejected", "summary": "标题", "message": "正文"}
    base.update(overrides)
    return base


def emit(settings: Settings, **overrides) -> dict:
    kw = notice_kwargs(**overrides)
    kind = kw.pop("kind")
    return write_notice(settings, kind=kind, payload=kw)


def test_notice_seq_is_monotonic(settings: Settings) -> None:
    seqs = [emit(settings)["seq"] for _ in range(5)]
    assert seqs == [1, 2, 3, 4, 5]


def test_notice_seq_survives_counter_loss(settings: Settings) -> None:
    """计数器文件被删了也不能倒退——否则会覆盖还没投递的事件。"""
    emit(settings)
    emit(settings)
    (settings.notify_dir / ".seq").unlink()
    assert emit(settings)["seq"] == 3


def test_notice_filename_is_sortable_by_seq(settings: Settings) -> None:
    emit(settings)
    paths = sorted(p.name for p in (settings.notify_dir / "pending").glob("*.json"))
    assert paths[0].startswith("000001-")


def test_notice_appends_audit_trail(settings: Settings) -> None:
    emit(settings)
    emit(settings)
    lines = (settings.notify_dir / "outbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["seq"] == 1


@pytest.mark.parametrize("bad", [{"kind": "nope"}, {"summary": ""}, {"message": ""}])
def test_notice_contract_validation(settings: Settings, bad: dict) -> None:
    with pytest.raises(ContractError):
        emit(settings, **bad)


def test_notice_keeps_member_and_doc_context(settings: Settings) -> None:
    write_notice(
        settings,
        kind="accepted",
        payload={
            "summary": "已受理",
            "message": "正文",
            "doc": {"doc_id": 11, "title": "新生见面会"},
            "member": {"name": "张三"},
        },
    )
    path = next((settings.notify_dir / "pending").glob("*.json"))
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["doc"]["doc_id"] == 11
    assert record["member"]["name"] == "张三"
    assert record["repo"] == "g/kb"


@pytest.mark.skipif(sys.platform == "win32", reason="文件锁在 Windows 上行为不同，生产环境是 Linux")
def test_seq_counter_is_atomic(settings: Settings) -> None:
    """回归：_next_seq() 必须用文件锁保护，避免并发写入产生重复序号。"""
    import threading

    from yuque_agent.outputs import _next_seq

    results: list[int] = []
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(10):
                seq = _next_seq(settings)
                results.append(seq)
        except Exception as e:
            errors.append(e)

    # 启动多个线程并发写入
    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写入出错：{errors}"
    # 50 次写入必须产生 50 个不同的序号
    assert len(results) == 50
    assert len(set(results)) == 50, f"有重复序号：{sorted(results)}"
    # 序号必须是 1~50
    assert set(results) == set(range(1, 51))
