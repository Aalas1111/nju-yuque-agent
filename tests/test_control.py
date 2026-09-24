"""控制请求队列测试：请求 → 消费 → 回执，以及 apply 的校验与落盘。**不联网、不跑 LLM。**"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from tests.fakes import FakeRunner, FakeRunResult
from yuque_agent import clock, control
from yuque_agent.config import Settings


def make_settings(tmp_path: Path) -> Settings:
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws")
    settings.ensure_dirs()
    return settings


def write_request(settings: Settings, payload: dict) -> Path:
    name = control.new_request_name(str(payload.get("kind") or "once"))
    path = settings.control_requests_dir / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def receipt(settings: Settings) -> dict:
    files = sorted(settings.control_done_dir.glob("*.json"))
    assert files, "没有回执"
    return json.loads(files[-1].read_text(encoding="utf-8"))


# ---------------------------------------------------------------- once / archive


def test_process_pending_runs_once_and_writes_receipt(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    runner = FakeRunner(
        poll_results=[FakeRunResult(verdict="accepted", summary="受理了《新生见面会》")]
    )
    write_request(settings, {"kind": "once", "requested_by": "u-1"})

    results = control.process_pending(settings, runner=runner, log=lambda _t: None)

    assert results[0]["ok"] is True
    assert runner.polls == 1
    assert runner.force_flags == [True]  # 人工命令：无视 diff 也要跑
    assert runner.debounce_flags == [False]  # 且关掉静默期
    data = receipt(settings)
    assert data["ok"] is True
    assert data["run_id"]  # 回执里有 run_id，请求方能给出「run: …」
    assert not list(settings.control_requests_dir.glob("*.json"))  # 请求已消费


def test_process_pending_archive_kind(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    runner = FakeRunner()
    write_request(settings, {"kind": "archive", "requested_by": "u-1"})
    control.process_pending(settings, runner=runner, log=lambda _t: None)
    assert runner.archives == 1
    assert receipt(settings)["ok"] is True


def test_no_change_receipt_says_zero_token(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_request(settings, {"kind": "once"})
    control.process_pending(settings, runner=FakeRunner(poll_results=[None]), log=lambda _t: None)
    data = receipt(settings)
    assert data["ok"] is True
    assert "没有变化" in data["summary"]


def test_runner_failure_becomes_error_receipt(tmp_path: Path) -> None:
    class BoomRunner(FakeRunner):
        def poll_once(self, **kwargs):  # noqa: ANN003
            raise RuntimeError("模拟 LLM 超时")

    settings = make_settings(tmp_path)
    write_request(settings, {"kind": "once"})
    control.process_pending(settings, runner=BoomRunner(), log=lambda _t: None)
    data = receipt(settings)
    assert data["ok"] is False
    assert "模拟 LLM 超时" in data["error"]


def test_malformed_request_is_moved_aside(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (settings.control_requests_dir / "20260924-120000-once-badbad.json").write_text(
        "{半截 JSON", encoding="utf-8"
    )
    results = control.process_pending(settings, runner=FakeRunner(), log=lambda _t: None)
    assert results == []  # 坏文件不阻塞队列
    assert list(settings.control_done_dir.glob("*.malformed.json"))


def test_unknown_kind_is_rejected_with_receipt(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_request(settings, {"kind": "launch-missiles"})
    control.process_pending(settings, runner=FakeRunner(), log=lambda _t: None)
    data = receipt(settings)
    assert data["ok"] is False
    assert "未知 kind" in data["error"]


# ---------------------------------------------------------------- apply


def _valid_raw(**overrides) -> dict:
    raw = {
        "activity_name": "学术讲座",
        "date": (clock.today() + timedelta(days=3)).isoformat(),
        "start": "14:00",
        "end": "16:00",
        "campus": "仙林",
        "people": 50,
    }
    raw.update(overrides)
    return raw


def test_apply_writes_application_and_accepted_notice(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    target = {"scope": "c2c", "target_id": "u-7"}

    outcome = control.apply_from_raw(
        settings, _valid_raw(), target=target, requested_by="u-7", log=lambda _t: None
    )

    assert outcome["ok"] is True
    app_path = settings.applications_dir / f"{outcome['application_id']}.json"
    app = json.loads(app_path.read_text(encoding="utf-8"))
    assert app["activity"]["title"] == "学术讲座"
    assert app["activity"]["campus"] == "3"  # 仙林
    assert app["activity"]["period"] == "5-6"  # 14:00-16:00 落在 5-6 节
    assert app["source"]["repo"] == "g/kb"
    assert app["source"]["title"] == "QQ申请-学术讲座"
    assert app["source"]["doc_id"]

    notices = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in (settings.notify_dir / "pending").glob("*.json")
    ]
    assert len(notices) == 1
    assert notices[0]["kind"] == "accepted"
    assert notices[0]["target"] == target  # 直投给申请人本人
    assert str(app["application_id"]) in notices[0]["message"]


def test_apply_rejects_bad_fields_with_rejected_notice(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    target = {"scope": "c2c", "target_id": "u-7"}

    outcome = control.apply_from_raw(
        settings,
        _valid_raw(date="2026-10-15", start="16:00", end="14:00", campus="月球"),
        target=target,
        requested_by="u-7",
        log=lambda _t: None,
    )

    assert outcome["ok"] is False
    assert len(outcome["errors"]) == 3  # 日期超范围 + 时间倒挂 + 认不出校区
    assert not list(settings.applications_dir.glob("*.json"))  # 没落盘
    notices = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in (settings.notify_dir / "pending").glob("*.json")
    ]
    assert notices[0]["kind"] == "rejected"
    assert notices[0]["target"] == target
    assert notices[0]["reasons"]


def test_apply_through_queue_needs_no_runner(tmp_path: Path) -> None:
    """apply 是程序性落盘，不需要 runner（核心进程就算没在跑也能处理）。"""
    settings = make_settings(tmp_path)
    write_request(
        settings,
        {
            "kind": "apply",
            "requested_by": "u-7",
            "target": {"scope": "c2c", "target_id": "u-7"},
            "raw": _valid_raw(),
        },
    )
    results = control.process_pending(settings, runner=None, log=lambda _t: None)
    assert results[0]["ok"] is True
    assert receipt(settings)["ok"] is True
    assert list(settings.applications_dir.glob("*.json"))


def test_apply_is_idempotent_for_same_person_and_day(tmp_path: Path) -> None:
    """同一人同一天重复提交覆盖同一个申请（和语雀侧「一篇文档只能有一份申请」一致）。"""
    settings = make_settings(tmp_path)
    first = control.apply_from_raw(settings, _valid_raw(), requested_by="u-7", log=lambda _t: None)
    second = control.apply_from_raw(
        settings, _valid_raw(activity_name="换个名字"), requested_by="u-7", log=lambda _t: None
    )
    assert first["application_id"] == second["application_id"]
    assert len(list(settings.applications_dir.glob("20*.json"))) == 1  # 不含 index.json
