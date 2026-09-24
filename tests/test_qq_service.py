"""常驻服务编排测试：控制请求、回执投递、通知泵、入站回复。**不联网、不跑 LLM。**"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from tests.qq_fakes import RecordingSender, make_notice
from yuque_agent.config import Settings
from yuque_agent.qqbot.bridge import NotifyBridge
from yuque_agent.qqbot.client import Target
from yuque_agent.qqbot.config import NotifyTarget, QQBotConfig
from yuque_agent.qqbot.events import InboundMessage
from yuque_agent.qqbot.service import QQBotService


def make_service(tmp_path: Path, *, sender=None, qq_client=None, config=None):
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws")
    config = config or QQBotConfig(
        notify_default=NotifyTarget("group", "g-1"),
        inbound_users=("u-admin",),
        inbound_admins=("u-admin",),
    )
    sender = sender if sender is not None else RecordingSender()
    bridge = NotifyBridge(notify_dir=settings.notify_dir, sender=sender, config=config)
    bridge.ensure_dirs()
    service = QQBotService(
        settings=settings,
        bridge=bridge,
        config=config,
        qq_client=qq_client,
        log=lambda _text: None,
    )
    return service, bridge, sender


def _request_files(service: QQBotService) -> list[Path]:
    return sorted(service.settings.control_requests_dir.glob("*.json"))


# ---------------------------------------------------------------- 控制请求


def test_request_run_writes_control_request(tmp_path: Path) -> None:
    """``/run`` 不再是进程内排队，而是给核心常驻进程写一条控制请求。"""
    service, _bridge, _sender = make_service(tmp_path)
    queued = service.request_run(
        requested_by="u-admin", reply_target=Target("c2c", "u-admin", msg_id="m-1")
    )
    assert queued["queued"] is True
    files = _request_files(service)
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["kind"] == "once"
    assert data["requested_by"] == "u-admin"
    assert data["target"] == {"scope": "c2c", "target_id": "u-admin"}


def test_archive_request_uses_archive_kind(tmp_path: Path) -> None:
    service, _bridge, _sender = make_service(tmp_path)
    service.request_run(archive=True, requested_by="u-admin")
    data = json.loads(_request_files(service)[0].read_text(encoding="utf-8"))
    assert data["kind"] == "archive"


def test_second_request_is_rejected_while_one_is_in_flight(tmp_path: Path) -> None:
    service, _bridge, _sender = make_service(tmp_path)
    assert service.request_run(requested_by="u-admin")["queued"] is True
    second = service.request_run(requested_by="u-admin")
    assert second["queued"] is False
    assert "还在跑" in second["message"]
    assert len(_request_files(service)) == 1  # 没有写第二条


def test_control_result_is_sent_back_to_requester(tmp_path: Path) -> None:
    """核心跑完写 ``control/done/``，通知泵取回来发给发起人，然后删掉、解除单飞。"""
    sender = RecordingSender()
    service, _bridge, _sender = make_service(tmp_path, qq_client=sender)
    queued = service.request_run(
        requested_by="u-admin", reply_target=Target("c2c", "u-admin", msg_id="m-1")
    )
    name = queued["request_id"]
    done_dir = service.settings.control_done_dir
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / name).write_text(
        json.dumps(
            {
                "ok": True,
                "summary": "受理了《新生见面会》",
                "run_id": "20260920-101834-polling-abcd",
                "error": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    service._drain_control_results()

    target, text = sender.sent[-1]
    assert target.scope == "c2c" and target.target_id == "u-admin"
    assert "受理了" in text
    assert "run: 20260920" in text
    assert not (done_dir / name).exists()  # 回执取走即删
    assert service.request_run(requested_by="u-admin")["queued"] is True  # 单飞解除


# ---------------------------------------------------------------- 通知泵


def test_notify_pump_drains_pending(tmp_path: Path) -> None:
    service, bridge, sender = make_service(tmp_path)
    make_notice(bridge.pending_dir / "000001-rejected-a.json", seq=1)
    service._drain_notices()
    assert len(sender.sent) == 1
    assert not (bridge.pending_dir / "000001-rejected-a.json").exists()


# ---------------------------------------------------------------- 入站


def test_handle_inbound_replies_via_qq_client(tmp_path: Path) -> None:
    sender = RecordingSender()
    service, _bridge, _sender = make_service(tmp_path, qq_client=sender)
    message = InboundMessage(kind="c2c", sender_id="u-admin", content="/help", message_id="m-1")
    result = service.handle_inbound(message)
    assert result["handled"] is True
    target, text = sender.sent[-1]
    assert target.scope == "c2c" and target.msg_id == "m-1"  # 被动回复
    assert "run" in text and "archive" in text  # 管理员看到的清单里有写操作


def test_handle_inbound_without_client_does_not_crash(tmp_path: Path) -> None:
    service, _bridge, _sender = make_service(tmp_path, qq_client=None)
    message = InboundMessage(kind="c2c", sender_id="u-admin", content="/status", message_id="m")
    result = service.handle_inbound(message)
    assert result["handled"] is True


def test_help_command_is_llm_free_and_lists_commands(tmp_path: Path) -> None:
    """显式命令直接命中，不过 LLM；非白名单用户拿不到命令清单。"""
    service, _bridge, _sender = make_service(tmp_path)
    admin = service.agent.process(
        InboundMessage(kind="c2c", sender_id="u-admin", content="/help", message_id="m"),
        settings=service.settings,
    )
    assert admin.command == "help"
    assert "run" in admin.reply

    stranger = service.agent.process(
        InboundMessage(kind="c2c", sender_id="u-nobody", content="/help", message_id="m"),
        settings=service.settings,
    )
    assert stranger.command == "denied"


# ---------------------------------------------------------------- 状态


def test_status_reports_pending_and_core_alive(tmp_path: Path) -> None:
    service, bridge, _sender = make_service(tmp_path)
    make_notice(bridge.pending_dir / "000001-a.json")
    status = service.status()
    assert status["repo"] == "g/kb"
    assert status["pending"] == 1
    assert status["inbound"] is False  # 没给 qq_client
    assert status["watching"] is False  # 还没有 state.json → 核心进程没在跑

    service.settings.root.mkdir(parents=True, exist_ok=True)
    (service.settings.root / "state.json").write_text("{}", encoding="utf-8")
    assert service.status()["watching"] is True


def test_last_run_summary_reads_newest_result(tmp_path: Path) -> None:
    service, _bridge, _sender = make_service(tmp_path)
    run_dir = service.settings.runs_dir / "20260920-101834-polling-abcd"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "kind": "polling",
                "verdict": "accepted",
                "summary": "受理了《新生见面会》",
                "run_id": "20260920-101834-polling-abcd",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    last = service.status()["last_run"]
    assert last["summary"] == "受理了《新生见面会》"
    assert last["run_id"] == "20260920-101834-polling-abcd"
    assert last["at"]  # 有 mtime 格式化出来的时间


def test_pending_notices_survives_missing_dir(tmp_path: Path) -> None:
    service, bridge, _sender = make_service(tmp_path)
    shutil.rmtree(bridge.notify_dir)
    assert service.pending_notices() == 0
