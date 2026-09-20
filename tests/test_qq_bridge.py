"""通知投递桥测试：done / unrouted / failed、顺序、至少一次、dry-run、审计。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.qq_fakes import RecordingSender, make_notice
from yuque_agent.config import Settings
from yuque_agent.qqbot.bridge import NotifyBridge
from yuque_agent.qqbot.config import NotifyTarget, QQBotConfig


def make_bridge(tmp_path: Path, *, sender=None, config=None, dry_run=False):
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws")
    bridge = NotifyBridge(
        notify_dir=settings.notify_dir,
        sender=sender or RecordingSender(),
        config=config or QQBotConfig(notify_default=NotifyTarget("group", "g-1")),
        dry_run=dry_run,
    )
    bridge.ensure_dirs()
    return bridge, settings


def test_delivers_and_moves_to_done(tmp_path: Path) -> None:
    sender = RecordingSender()
    bridge, _ = make_bridge(
        tmp_path, sender=sender, config=QQBotConfig(members={"张三": NotifyTarget("c2c", "u-1")})
    )
    path = make_notice(bridge.pending_dir / "000001-rejected-abc.json", seq=1)

    results = bridge.drain()

    assert len(results) == 1
    assert results[0].status == "delivered"
    assert results[0].target == "c2c:u-1"
    assert not path.exists()
    assert (bridge.done_dir / path.name).exists()
    assert sender.sent[0][1].startswith("「社团例会」")
    assert sender.sent[0][0].scope == "c2c"
    assert bridge.pending_count() == 0


def test_unmapped_member_goes_to_unrouted_when_skip(tmp_path: Path) -> None:
    sender = RecordingSender()
    bridge, _ = make_bridge(tmp_path, sender=sender, config=QQBotConfig(notify_unmapped="skip"))
    path = make_notice(bridge.pending_dir / "000001-rejected-abc.json")

    results = bridge.drain()

    assert results[0].status == "unrouted"
    assert results[0].reason == "skip"
    assert (bridge.unrouted_dir / path.name).exists()
    assert sender.sent == []


def test_unmapped_member_uses_default_target(tmp_path: Path) -> None:
    sender = RecordingSender()
    bridge, _ = make_bridge(tmp_path, sender=sender)
    make_notice(bridge.pending_dir / "000001-rejected-abc.json")

    bridge.drain()

    assert sender.sent[0][0].to_str() == "group:g-1"


def test_send_failure_keeps_file_pending_and_stops_the_queue(tmp_path: Path) -> None:
    sender = RecordingSender(fail_times=1)
    bridge, _ = make_bridge(tmp_path, sender=sender)
    first = make_notice(bridge.pending_dir / "000001-rejected-a.json", seq=1)
    second = make_notice(bridge.pending_dir / "000002-rejected-b.json", seq=2)

    results = bridge.drain()

    assert [item.status for item in results] == ["failed"]  # 停在第一条，不越过它
    assert first.exists() and second.exists()
    assert sender.calls == 1

    # 下一轮重试成功 → 两条都出去
    sender.fail_times = 0
    results = bridge.drain()
    assert [item.status for item in results] == ["delivered", "delivered"]
    assert not first.exists() and not second.exists()


def test_malformed_json_moves_to_failed(tmp_path: Path) -> None:
    bridge, _ = make_bridge(tmp_path)
    path = bridge.pending_dir / "000001-info-zzz.json"
    path.write_text("{ not json", encoding="utf-8")

    results = bridge.drain()

    assert results[0].status == "failed"
    assert results[0].reason == "malformed"
    assert (bridge.failed_dir / path.name).exists()
    assert bridge.pending_count() == 0


def test_notice_without_message_moves_to_failed(tmp_path: Path) -> None:
    bridge, _ = make_bridge(tmp_path)
    path = bridge.pending_dir / "000001-info-zzz.json"
    path.write_text(
        json.dumps({"seq": 1, "kind": "info", "member": {"name": "张三"}}), encoding="utf-8"
    )

    results = bridge.drain()

    assert results[0].status == "failed"
    assert results[0].reason == "empty_message"
    assert (bridge.failed_dir / path.name).exists()


def test_ordering_follows_seq_not_filename(tmp_path: Path) -> None:
    sender = RecordingSender()
    bridge, _ = make_bridge(tmp_path, sender=sender)
    make_notice(bridge.pending_dir / "000002-b.json", seq=2, message="second")
    make_notice(bridge.pending_dir / "000001-a.json", seq=1, message="first")

    bridge.drain()

    assert [text for _target, text in sender.sent] == ["first", "second"]


def test_dry_run_sends_nothing_and_moves_nothing(tmp_path: Path) -> None:
    sender = RecordingSender()
    bridge, _ = make_bridge(tmp_path, sender=sender, dry_run=True)
    path = make_notice(bridge.pending_dir / "000001-a.json")

    results = bridge.drain()

    assert results[0].status == "dry_run"
    assert sender.sent == []
    assert path.exists()


def test_deliver_single_file(tmp_path: Path) -> None:
    bridge, _ = make_bridge(tmp_path)
    path = make_notice(bridge.pending_dir / "000007-info-x.json", seq=7)
    result = bridge.deliver(path)
    assert result.status == "delivered"
    assert result.seq == 7


def test_audit_log_is_appended(tmp_path: Path) -> None:
    bridge, _ = make_bridge(tmp_path)
    make_notice(bridge.pending_dir / "000001-a.json", seq=1)
    bridge.drain()
    lines = bridge.audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["status"] == "delivered"
    assert record["seq"] == 1
    assert record["at"]


def test_stats_counts_each_folder(tmp_path: Path) -> None:
    bridge, _ = make_bridge(tmp_path)
    make_notice(bridge.pending_dir / "000001-a.json", seq=1)
    assert bridge.stats() == {"pending": 1, "done": 0, "unrouted": 0, "failed": 0}
    bridge.drain()
    assert bridge.stats() == {"pending": 0, "done": 1, "unrouted": 0, "failed": 0}


def test_drain_on_empty_dir_is_noop(tmp_path: Path) -> None:
    bridge, _ = make_bridge(tmp_path)
    assert bridge.drain() == []


def test_limit_caps_how_many_go_out(tmp_path: Path) -> None:
    sender = RecordingSender()
    bridge, _ = make_bridge(tmp_path, sender=sender)
    for seq in range(1, 5):
        make_notice(bridge.pending_dir / f"{seq:06d}-a.json", seq=seq)
    results = bridge.drain(limit=2)
    assert len(results) == 2
    assert bridge.pending_count() == 2


def test_describe_messages() -> None:
    from yuque_agent.qqbot.bridge import DeliveryResult

    delivered = DeliveryResult(
        path=Path("x.json"), seq=3, kind="info", status="delivered", target="c2c:u"
    )
    assert "已投递" in delivered.describe()
    failed = DeliveryResult(path=Path("x.json"), seq=3, kind="info", status="failed", error="boom")
    assert "boom" in failed.describe()
    unrouted = DeliveryResult(path=Path("x.json"), seq=3, status="unrouted", reason="skip")
    assert "unrouted" in unrouted.describe()
