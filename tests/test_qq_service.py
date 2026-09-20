"""常驻服务编排测试：队列、单飞、通知泵、入站回复。**不联网、不跑 LLM。**"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from tests.qq_fakes import FakeRunner, FakeRunResult, FakeWatcher, RecordingSender, make_notice
from yuque_agent.config import Settings
from yuque_agent.qqbot.bridge import NotifyBridge
from yuque_agent.qqbot.config import NotifyTarget, QQBotConfig
from yuque_agent.qqbot.events import InboundMessage
from yuque_agent.qqbot.service import QQBotService


def make_service(tmp_path: Path, *, runner=None, sender=None, qq_client=None, config=None):
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws")
    runner = runner or FakeRunner()
    watcher = FakeWatcher()
    config = config or QQBotConfig(
        notify_default=NotifyTarget("group", "g-1"),
        inbound_allow=("u-admin",),
        inbound_admins=("u-admin",),
    )
    sender = sender or RecordingSender()
    bridge = NotifyBridge(notify_dir=settings.notify_dir, sender=sender, config=config)
    bridge.ensure_dirs()
    service = QQBotService(
        settings=settings,
        runner=runner,
        watcher=watcher,
        bridge=bridge,
        config=config,
        qq_client=qq_client,
        log=lambda _text: None,
    )
    return service, runner, watcher, bridge, sender


def run_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------- 队列


def test_request_run_is_queued_and_executed_by_worker(tmp_path: Path) -> None:
    service, runner, _watcher, _bridge, _sender = make_service(tmp_path)
    queued = service.request_run(requested_by="u-admin")
    assert queued["queued"] is True

    worker = threading.Thread(target=service.work_loop, args=(False,), daemon=True)
    worker.start()
    try:
        assert run_until(lambda: runner.polls >= 1)
    finally:
        service.stop()
        worker.join(timeout=5)
    assert runner.force_flags == [True]  # /run 是「无视 diff 也要跑」
    # 而且必须**关掉静默期**：/run 是管理员在手机上敲的人工命令，敲了就该立刻看到结果，
    # 不该被静默期吃掉后回头告诉他「这一轮没有变化」（那是假话）。
    assert runner.debounce_flags == [False]


def test_second_request_is_rejected_while_busy(tmp_path: Path) -> None:
    release = threading.Event()

    class BlockingRunner(FakeRunner):
        def poll_once(self, **kwargs):  # noqa: ANN003
            self.polls += 1
            release.wait(timeout=5)
            return FakeRunResult()

    service, _runner, _watcher, _bridge, _sender = make_service(tmp_path, runner=BlockingRunner())
    assert service.request_run(requested_by="u-admin")["queued"] is True
    worker = threading.Thread(target=service.work_loop, args=(False,), daemon=True)
    worker.start()
    try:
        assert run_until(lambda: service.status()["busy"])
        second = service.request_run(requested_by="u-admin")
        assert second["queued"] is False
        assert "正在忙" in second["message"]
    finally:
        release.set()
        service.stop()
        worker.join(timeout=5)


def test_archive_request_calls_archive_once(tmp_path: Path) -> None:
    service, runner, _watcher, _bridge, _sender = make_service(tmp_path)
    service.request_run(archive=True, requested_by="u-admin")
    worker = threading.Thread(target=service.work_loop, args=(False,), daemon=True)
    worker.start()
    try:
        assert run_until(lambda: runner.archives >= 1)
    finally:
        service.stop()
        worker.join(timeout=5)


def test_result_is_pushed_back_to_requester(tmp_path: Path) -> None:
    sender = RecordingSender()
    runner = FakeRunner(
        poll_results=[FakeRunResult(verdict="accepted", summary="受理了《新生见面会》")]
    )
    service, _runner, _watcher, _bridge, _sender = make_service(
        tmp_path, runner=runner, sender=sender, qq_client=sender
    )
    service.request_run(requested_by="u-admin")
    worker = threading.Thread(target=service.work_loop, args=(False,), daemon=True)
    worker.start()
    try:
        assert run_until(lambda: any("受理了" in text for _t, text in sender.sent))
    finally:
        service.stop()
        worker.join(timeout=5)
    target, text = sender.sent[-1]
    assert target.scope == "c2c" and target.target_id == "u-admin"
    assert "run: 20260920" in text


def test_no_change_run_announces_zero_token(tmp_path: Path) -> None:
    sender = RecordingSender()
    service, _runner, _watcher, _bridge, _sender = make_service(
        tmp_path, runner=FakeRunner(poll_results=[None]), sender=sender, qq_client=sender
    )
    service.request_run(requested_by="u-admin")
    worker = threading.Thread(target=service.work_loop, args=(False,), daemon=True)
    worker.start()
    try:
        assert run_until(lambda: any("没有变化" in text for _t, text in sender.sent))
    finally:
        service.stop()
        worker.join(timeout=5)


def test_work_loop_with_watch_ticks_the_watcher(tmp_path: Path) -> None:
    service, _runner, watcher, _bridge, _sender = make_service(tmp_path)
    service.settings.interval = 1
    worker = threading.Thread(target=service.work_loop, args=(True,), daemon=True)
    worker.start()
    try:
        assert run_until(lambda: watcher.ticks >= 1)
    finally:
        service.stop()
        worker.join(timeout=5)
    assert service.status()["watching"] is True


def test_work_loop_drains_notifications(tmp_path: Path) -> None:
    sender = RecordingSender()
    service, _runner, _watcher, bridge, _sender = make_service(tmp_path, sender=sender)
    service.settings.interval = 1
    make_notice(bridge.pending_dir / "000001-rejected-a.json", seq=1)
    worker = threading.Thread(target=service.work_loop, args=(False,), daemon=True)
    worker.start()
    try:
        assert run_until(lambda: len(sender.sent) == 1)
    finally:
        service.stop()
        worker.join(timeout=5)


# ---------------------------------------------------------------- 入站


def test_handle_inbound_replies_via_qq_client(tmp_path: Path) -> None:
    sender = RecordingSender()
    service, _runner, _watcher, _bridge, _sender = make_service(tmp_path, qq_client=sender)
    message = InboundMessage(kind="c2c", sender_id="u-admin", content="/help", message_id="m-1")
    result = service.handle_inbound(message)
    assert result["handled"] is True
    target, text = sender.sent[-1]
    assert target.scope == "c2c" and target.msg_id == "m-1"  # 被动回复
    assert "/run" in text


def test_handle_inbound_without_client_does_not_crash(tmp_path: Path) -> None:
    service, _runner, _watcher, _bridge, _sender = make_service(tmp_path, qq_client=None)
    message = InboundMessage(kind="c2c", sender_id="u-admin", content="/status", message_id="m")
    result = service.handle_inbound(message)
    assert result["handled"] is True


def test_status_reports_pending_and_config(tmp_path: Path) -> None:
    service, _runner, _watcher, bridge, _sender = make_service(tmp_path)
    make_notice(bridge.pending_dir / "000001-a.json")
    status = service.status()
    assert status["repo"] == "g/kb"
    assert status["pending"] == 1
    assert status["inbound"] is False  # 没给 qq_client
    assert status["busy"] is False


def test_pending_notices_survives_missing_dir(tmp_path: Path) -> None:
    service, _runner, _watcher, bridge, _sender = make_service(tmp_path)
    import shutil

    shutil.rmtree(bridge.notify_dir)
    assert service.pending_notices() == 0
