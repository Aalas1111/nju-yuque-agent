"""入站命令路由测试：白名单、管理员、限流、单飞、help/status/pending。"""

from __future__ import annotations

from typing import Any

from yuque_agent.qqbot.commands import HELP_TEXT, CommandRouter, render_status, whoami_text
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
            inbound_user_groups=("g-1",),
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


def test_unlisted_group_speaker_is_silent() -> None:
    """群不在任何群名单里、人也不在名单里 → 群里静默不回（不打扰大家）。"""
    router, _, _ = make_router()
    msg = InboundMessage(
        kind="group",
        sender_id="m-stranger",
        group_openid="g-999",
        content="/status",
        message_id="m",
    )
    result = router.dispatch(msg)
    assert result.handled is False
    assert result.silent is True
    assert result.reply == ""


def test_anyone_in_user_group_is_a_user() -> None:
    """「用户直接从用户群读取」：用户群里谁发言都算用户，不用逐个登记 openid。"""
    router, _, _ = make_router()  # make_router 把 g-1 配成 user_groups
    for sender in ("m-1", "m-2", "m-完全没登记过"):
        msg = InboundMessage(
            kind="group", sender_id=sender, group_openid="g-1", content="/status", message_id="m"
        )
        assert router.dispatch(msg).handled is True


def test_individual_grant_still_works_inside_an_unlisted_group() -> None:
    """个人名单与群名单各自独立生效：人在 allow 里，群没登记也放行。"""
    router, _, _ = make_router()
    msg = InboundMessage(
        kind="group", sender_id="u-1", group_openid="g-999", content="/status", message_id="m"
    )
    assert router.dispatch(msg).handled is True


def test_admin_group_makes_everyone_admin() -> None:
    """管理员群里谁发言都是管理员（能用 /archive）——文档里明确警告过风险。"""
    config = QQBotConfig(
        inbound_allow=("u-admin",),
        inbound_admins=("u-admin",),
        inbound_user_groups=("g-1",),
        inbound_admin_groups=("g-admin",),
        inbound_rate_limit=0,
    )
    router, gateway, _ = make_router(config=config)
    msg = InboundMessage(
        kind="group",
        sender_id="m-anyone",
        group_openid="g-admin",
        content="/archive",
        message_id="m",
    )
    result = router.dispatch(msg)
    assert result.handled is True
    assert result.admin is True
    assert gateway.calls[-1]["archive"] is True


def test_user_group_cannot_run_write_commands() -> None:
    """用户群里的人只是「用户」：/run 会被拒。"""
    router, gateway, _ = make_router()  # g-1 = user_groups
    msg = InboundMessage(
        kind="group", sender_id="m-1", group_openid="g-1", content="/run", message_id="m"
    )
    result = router.dispatch(msg)
    assert result.handled is False
    assert "只有管理员" in result.reply
    assert gateway.calls == []


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

    # ------------------------------------------------------------ 引导


def test_whoami_works_before_being_allowlisted() -> None:
    """``/whoami`` 必须在白名单之前放行——否则第一次用的人拿不到自己的 openid。"""
    router, _gateway, _ = make_router(config=QQBotConfig())  # allow 为空 = 默认拒绝
    result = router.dispatch(c2c("/whoami", sender="U-stranger"))

    assert result.handled is True
    assert result.command == "whoami"
    assert "U-stranger" in result.reply


def test_whoami_aliases_work() -> None:
    router, _gateway, _ = make_router(config=QQBotConfig())
    for text in ("/myid", "我是谁", "id"):
        assert router.dispatch(c2c(text, sender="U-x")).command == "whoami"


def test_whoami_in_group_gives_both_ids() -> None:
    router, _gateway, _ = make_router(config=QQBotConfig())
    msg = InboundMessage(
        kind="group", sender_id="M-xyz", group_openid="G-123", content="/whoami", message_id="m"
    )
    result = router.dispatch(msg)

    assert result.handled is True
    assert "G-123" in result.reply and "M-xyz" in result.reply


def test_denial_for_direct_message_shows_your_own_openid() -> None:
    """被拒时把自己的 openid 给他（是事实，不是让他去翻配置）。"""
    router, _gateway, _ = make_router(config=QQBotConfig())
    result = router.dispatch(c2c("你好", sender="U-stranger"))

    assert result.handled is False
    assert "U-stranger" in result.reply


def test_replies_are_information_only_never_config_guides() -> None:
    """命令只回信息，不回指南：回复里不该出现配置片段或「怎么改配置」的说明。"""
    forbidden = ('"allow"', '"admins"', '"user_groups"', '"admin_groups"', "填进", "重启服务")
    router, _gateway, _ = make_router(config=QQBotConfig())
    replies = [
        router.dispatch(c2c("/whoami", sender="U-stranger")).reply,
        router.dispatch(c2c("你好", sender="U-stranger")).reply,
        router.dispatch(
            InboundMessage(
                kind="group",
                sender_id="M-xyz",
                group_openid="G-123",
                content="/whoami",
                message_id="m",
            )
        ).reply,
    ]
    for reply in replies:
        for bad in forbidden:
            assert bad not in reply, f"{bad!r} 出现在回复里：{reply}"


def test_whoami_text_never_leaks_other_peoples_ids() -> None:
    msg = InboundMessage(kind="c2c", sender_id="U-mine", content="/whoami", message_id="m")
    text = whoami_text(msg)
    assert "U-mine" in text
    assert "U-other" not in text


def test_help_lists_whoami() -> None:
    assert "/whoami" in HELP_TEXT


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
