"""入站命令路由测试：白名单、管理员、限流、单飞、help/status/pending。"""

from __future__ import annotations

from typing import Any

from yuque_agent.qqbot.commands import HELP_TEXT, CommandRouter, render_status
from yuque_agent.qqbot.config import QQBotConfig
from yuque_agent.qqbot.events import InboundMessage


class FakeGateway:
    def __init__(self, *, queued: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self.queued = queued

    def status(self) -> dict[str, Any]:
        return {
            "repo": "g/kb",
            "watching": True,
            "pending": 2,
            "inbound": True,
            "last_run": {
                "kind": "polling",
                "verdict": "accepted",
                "summary": "受理了 1 篇申请",
                "run_id": "20260920-101834-polling-abcd",
                "at": "2026-09-20T10:18:34+08:00",
            },
        }

    def pending_notices(self) -> int:
        return 2

    def request_run(
        self, *, archive: bool = False, requested_by: str = "", reply_target: Any = None
    ) -> dict[str, Any]:
        self.calls.append(
            {"archive": archive, "requested_by": requested_by, "reply_target": reply_target}
        )
        if not self.queued:
            return {"queued": False, "message": "agent 正在忙，等它跑完再来。"}
        return {"queued": True, "message": "收到，已排队跑一轮轮询；跑完我把结论发给你。"}


def make_router(*, config: QQBotConfig | None = None, gateway=None, clock=None):
    gateway = gateway or FakeGateway()
    now = {"t": 1000.0}
    router = CommandRouter(
        config=config
        or QQBotConfig(
            inbound_allow=("u-1", "u-admin"),
            inbound_admins=("u-admin",),
            inbound_groups=("g-1",),
            inbound_rate_limit=30,
        ),
        gateway=gateway,
        clock=clock or (lambda: now["t"]),
    )
    return router, gateway, now


def c2c(text: str, sender: str = "u-1") -> InboundMessage:
    return InboundMessage(kind="c2c", sender_id=sender, content=text, message_id="m-1")


# ---------------------------------------------------------------- 权限


def test_denies_everyone_when_allowlist_empty() -> None:
    router, gateway, _ = make_router(config=QQBotConfig())
    result = router.dispatch(c2c("/status"))
    assert result.handled is False
    assert "白名单" in result.reply
    assert gateway.calls == []


def test_unknown_sender_is_denied() -> None:
    router, _, _ = make_router()
    result = router.dispatch(c2c("/status", sender="u-stranger"))
    assert result.handled is False
    assert "白名单" in result.reply


def test_group_speaker_outside_allowlist_is_silent() -> None:
    router, _, _ = make_router()
    msg = InboundMessage(
        kind="group", sender_id="u-stranger", group_openid="g-1", content="/status", message_id="m"
    )
    result = router.dispatch(msg)
    assert result.handled is False
    assert result.silent is True
    assert result.reply == ""


def test_group_must_be_allowlisted_too() -> None:
    router, _, _ = make_router()
    msg = InboundMessage(
        kind="group", sender_id="u-1", group_openid="g-999", content="/status", message_id="m"
    )
    assert router.dispatch(msg).handled is False


# ---------------------------------------------------------------- 读命令


def test_help_command() -> None:
    router, _, _ = make_router()
    result = router.dispatch(c2c("/help"))
    assert result.handled is True
    assert result.reply == HELP_TEXT
    assert "/run" in result.reply


def test_chinese_alias_works_without_slash() -> None:
    router, _, _ = make_router()
    result = router.dispatch(c2c("状态"))
    assert result.handled is True
    assert "g/kb" in result.reply


def test_status_command_renders_last_run() -> None:
    router, _, _ = make_router()
    result = router.dispatch(c2c("/status"))
    assert "知识库：g/kb" in result.reply
    assert "待投递通知：2 条" in result.reply
    assert "受理了 1 篇申请" in result.reply


def test_pending_command() -> None:
    router, _, _ = make_router()
    assert "2 条" in router.dispatch(c2c("/pending")).reply


def test_free_text_is_not_sent_to_llm() -> None:
    router, gateway, _ = make_router()
    result = router.dispatch(c2c("帮我把这篇文档删了"))
    assert result.handled is False
    assert "只认命令" in result.reply
    assert gateway.calls == []


def test_empty_message_is_silent() -> None:
    router, _, _ = make_router()
    result = router.dispatch(c2c("   "))
    assert result.silent is True


# ---------------------------------------------------------------- 写命令


def test_admin_can_queue_a_run() -> None:
    router, gateway, _ = make_router()
    result = router.dispatch(c2c("/run", sender="u-admin"))
    assert result.handled is True
    assert result.admin is True
    assert "已排队" in result.reply
    assert gateway.calls[0]["archive"] is False
    assert gateway.calls[0]["requested_by"] == "u-admin"
    assert gateway.calls[0]["reply_target"] is not None  # 带上 msg_id，播报才能做被动回复


def test_non_admin_cannot_run() -> None:
    router, gateway, _ = make_router()
    result = router.dispatch(c2c("/run", sender="u-1"))
    assert result.handled is False
    assert "只有管理员" in result.reply
    assert gateway.calls == []


def test_archive_is_admin_only_and_passes_flag() -> None:
    router, gateway, _ = make_router()
    router.dispatch(c2c("/archive", sender="u-admin"))
    assert gateway.calls[-1]["archive"] is True


def test_busy_gateway_reports_backpressure() -> None:
    router, _, _ = make_router(gateway=FakeGateway(queued=False))
    result = router.dispatch(c2c("/run", sender="u-admin"))
    assert "正在忙" in result.reply


def test_admin_commands_are_rate_limited_per_sender() -> None:
    router, gateway, now = make_router()
    first = router.dispatch(c2c("/run", sender="u-admin"))
    assert first.handled is True
    second = router.dispatch(c2c("/run", sender="u-admin"))
    assert second.handled is False
    assert "秒后再来" in second.reply
    assert len(gateway.calls) == 1

    now["t"] += 31  # 过了限流窗口
    assert router.dispatch(c2c("/run", sender="u-admin")).handled is True
    assert len(gateway.calls) == 2


def test_read_commands_are_not_rate_limited() -> None:
    router, _, _ = make_router()
    assert router.dispatch(c2c("/status")).handled is True
    assert router.dispatch(c2c("/status")).handled is True


# ---------------------------------------------------------------- 渲染


def test_render_status_handles_empty_payload() -> None:
    text = render_status({})
    assert "最近一轮：还没有跑过" in text


def test_command_result_to_dict() -> None:
    router, _, _ = make_router()
    payload = router.dispatch(c2c("/help")).to_dict()
    assert payload["handled"] is True and payload["command"] == "help"
