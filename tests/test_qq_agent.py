"""RouterAgent 测试：显式命令快路、白名单、限流、/apply 开场。**不联网、不跑 LLM。**

合并前（``CommandRouter`` 时代）那批命令行为用例都在这里——路由换成了 Agent，
但「谁能用、用什么、限流怎么算」这些**行为**不变。
"""

from __future__ import annotations

from pathlib import Path

from yuque_agent.config import Settings
from yuque_agent.qqbot.agent import RouterAgent, register_default_workflows
from yuque_agent.qqbot.config import QQBotConfig
from yuque_agent.qqbot.conversations import ConversationManager
from yuque_agent.qqbot.events import InboundMessage


class FakeGateway:
    """假的 service：只记录调用，不碰网络。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def status(self) -> dict:
        return {"repo": "g/kb", "watching": True, "pending": 2}

    def pending_notices(self) -> int:
        return 3

    def request_run(
        self, *, archive: bool = False, requested_by: str = "", reply_target=None
    ) -> dict:
        self.calls.append(("archive" if archive else "run", requested_by))
        return {"queued": True, "message": "已排队", "request_id": "req-1"}

    def request_apply(self, *, user_id: str) -> dict:
        self.calls.append(("apply", user_id))
        return {"message": "开始填写教室借用申请。"}


def make_agent(
    tmp_path: Path,
    *,
    gateway: FakeGateway | None = None,
    rate_limit: int = 30,
    **config_kwargs,
):
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws")
    config = QQBotConfig(
        inbound_users=("u-1",),
        inbound_admins=("u-admin",),
        inbound_rate_limit=rate_limit,
        **config_kwargs,
    )
    gateway = gateway or FakeGateway()
    manager = ConversationManager(conversations_dir=settings.conversations_dir)
    agent = RouterAgent(
        conversations=manager, config=config, gateway=gateway, log=lambda _text: None
    )
    register_default_workflows(agent)
    return agent, settings, gateway, manager


def msg(text: str, *, sender: str = "u-1", kind: str = "c2c") -> InboundMessage:
    return InboundMessage(kind=kind, sender_id=sender, content=text, message_id="m-1")


def no_llm(monkeypatch, manager: ConversationManager) -> None:
    """把 LLM 挂掉——显式命令必须照样能用。"""

    def boom(_settings):  # noqa: ANN001
        raise AssertionError("这条路径不该调 LLM")

    monkeypatch.setattr(manager, "_get_llm", boom)


# ---------------------------------------------------------------- 白名单


def test_stranger_is_denied(tmp_path: Path) -> None:
    agent, settings, _gateway, _manager = make_agent(tmp_path)
    result = agent.process(msg("/help", sender="u-nobody"), settings=settings)
    assert result.command == "denied"
    assert "白名单" in result.reply


def test_empty_message_is_silent(tmp_path: Path) -> None:
    agent, settings, _gateway, _manager = make_agent(tmp_path)
    result = agent.process(msg("   "), settings=settings)
    assert result.silent is True and result.reply == ""


# ---------------------------------------------------------------- 显式命令（零 LLM）


def test_status_command_runs_without_llm(tmp_path: Path, monkeypatch) -> None:
    agent, settings, _gateway, manager = make_agent(tmp_path)
    no_llm(monkeypatch, manager)
    result = agent.process(msg("/status"), settings=settings)
    assert result.command == "status"
    assert "知识库：g/kb" in result.reply
    assert "待投递通知：2 条" in result.reply


def test_chinese_alias_works_without_slash(tmp_path: Path, monkeypatch) -> None:
    agent, settings, gateway, manager = make_agent(tmp_path)
    no_llm(monkeypatch, manager)
    result = agent.process(msg("状态"), settings=settings)
    assert result.command == "status"
    assert gateway.calls == []


def test_pending_command(tmp_path: Path, monkeypatch) -> None:
    agent, settings, _gateway, manager = make_agent(tmp_path)
    no_llm(monkeypatch, manager)
    result = agent.process(msg("/pending"), settings=settings)
    assert result.command == "pending"
    assert "3 条通知" in result.reply


def test_help_is_role_aware(tmp_path: Path, monkeypatch) -> None:
    agent, settings, _gateway, manager = make_agent(tmp_path)
    no_llm(monkeypatch, manager)

    user_help = agent.process(msg("/help"), settings=settings)
    assert user_help.command == "help"
    assert "管理员" not in user_help.reply or "仅管理员" not in user_help.reply
    assert "run" not in user_help.reply

    admin_help = agent.process(msg("/help", sender="u-admin"), settings=settings)
    assert "run" in admin_help.reply and "archive" in admin_help.reply


# ---------------------------------------------------------------- 写操作权限与限流


def test_user_cannot_run_write_commands(tmp_path: Path, monkeypatch) -> None:
    agent, settings, gateway, manager = make_agent(tmp_path)
    no_llm(monkeypatch, manager)
    result = agent.process(msg("/run"), settings=settings)
    assert "只有管理员能用" in result.reply
    assert gateway.calls == []


def test_admin_can_run(tmp_path: Path, monkeypatch) -> None:
    agent, settings, gateway, manager = make_agent(tmp_path)
    no_llm(monkeypatch, manager)
    result = agent.process(msg("/run", sender="u-admin"), settings=settings)
    assert result.command == "run"
    assert gateway.calls == [("run", "u-admin")]


def test_admin_archive_passes_through(tmp_path: Path, monkeypatch) -> None:
    agent, settings, gateway, manager = make_agent(tmp_path)
    no_llm(monkeypatch, manager)
    agent.process(msg("/archive", sender="u-admin"), settings=settings)
    assert gateway.calls == [("archive", "u-admin")]


def test_admin_writes_are_rate_limited(tmp_path: Path, monkeypatch) -> None:
    agent, settings, gateway, manager = make_agent(tmp_path, rate_limit=30)
    no_llm(monkeypatch, manager)
    first = agent.process(msg("/run", sender="u-admin"), settings=settings)
    assert first.command == "run"
    second = agent.process(msg("/run", sender="u-admin"), settings=settings)
    assert "刚跑过" in second.reply
    assert gateway.calls == [("run", "u-admin")]  # 第二次没到 gateway


def test_read_commands_are_not_rate_limited(tmp_path: Path, monkeypatch) -> None:
    agent, settings, _gateway, manager = make_agent(tmp_path, rate_limit=30)
    no_llm(monkeypatch, manager)
    for _ in range(3):
        result = agent.process(msg("/status"), settings=settings)
        assert result.command == "status"


# ---------------------------------------------------------------- /apply 与会话


def test_apply_command_starts_interactive_session(tmp_path: Path, monkeypatch) -> None:
    agent, settings, gateway, manager = make_agent(tmp_path)
    no_llm(monkeypatch, manager)
    result = agent.process(msg("/apply"), settings=settings)
    assert result.command == "booking"
    assert gateway.calls == [("apply", "u-1")]
    assert "教室借用" in result.reply


def test_active_session_takes_over_messages(tmp_path: Path, monkeypatch) -> None:
    agent, settings, _gateway, manager = make_agent(tmp_path)
    no_llm(monkeypatch, manager)
    manager.get_or_create("u-1")  # 有一个在填的会话
    result = agent.process(msg("取消"), settings=settings)
    assert result.command == "booking"
    assert "已取消" in result.reply


# ---------------------------------------------------------------- 兜底


def test_unknown_text_falls_back_to_help_when_llm_unavailable(tmp_path: Path, monkeypatch) -> None:
    agent, settings, _gateway, manager = make_agent(tmp_path)
    monkeypatch.setattr(manager, "_get_llm", lambda _settings: None)
    result = agent.process(msg("帮我借一个容纳十个人的教室"), settings=settings)
    assert result.command == "unknown"
    assert "可用功能" in result.reply
