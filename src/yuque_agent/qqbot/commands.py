"""入站命令路由——**QQ 侧的能力边界也在这里**。

本项目的研究立场是「安全的第一道闸门是本次会话注册了哪些工具」。
QQ 侧同理：**能不能让 agent 干活，由配置里的白名单决定，不由提示词决定**。

* ``/help`` ``/status`` ``/pending``：只读，白名单里的任何人都能用；
* ``/run`` ``/archive``：会让 agent 跑 LLM（花 token）甚至改知识库结构，
  **只有 ``inbound.admins`` 里的人能用**，而且有限流 + 单飞（正在跑就拒绝）；
* 其他任何文本**不会**被当成提示词发给 LLM（那等于把能力边界让位给提示词），
  只会回一句「我只认命令，发 /help 看看」。

默认拒绝：``inbound.allow`` 为空时，**谁的命令都不接受**。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .config import ROLE_ADMIN, ROLE_USER, QQBotConfig
from .events import InboundMessage

_SLASH_RE = re.compile(r"^[/／!！]\s*(\S+)")


@dataclass(frozen=True)
class CommandSpec:
    name: str
    help: str
    admin_only: bool = False
    aliases: tuple[str, ...] = ()


#: 命令表。``aliases`` 里放了中文写法，手机上不打斜杠也能用。
COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "whoami",
        "把你的 openid（群里还会给群 openid）打出来——加白名单用，**不需要先在白名单里**",
        aliases=("myid", "我是谁", "id"),
    ),
    CommandSpec("help", "看看我能做什么", aliases=("帮助", "?", "？", "h")),
    CommandSpec("status", "看一眼知识库与 agent 的状态", aliases=("状态", "s")),
    CommandSpec("pending", "还有几条通知没投递出去", aliases=("待投递", "通知", "p")),
    CommandSpec(
        "run",
        "立刻跑一轮轮询（仅管理员；会花 token）",
        admin_only=True,
        aliases=("跑一轮", "once", "r"),
    ),
    CommandSpec(
        "archive",
        "立刻跑一次归档会话（仅管理员；会改知识库结构）",
        admin_only=True,
        aliases=("归档", "a"),
    ),
)

_COMMAND_INDEX: dict[str, CommandSpec] = {}
for _spec in COMMANDS:
    _COMMAND_INDEX[_spec.name] = _spec
    for _alias in _spec.aliases:
        _COMMAND_INDEX[_alias] = _spec


HELP_TEXT = "\n".join(
    [
        "我是语雀知识库的看门 agent，能做的事：",
        *[
            f"  /{spec.name} —— {spec.help}" + ("（仅管理员）" if spec.admin_only else "")
            for spec in COMMANDS
        ],
        "我只认命令，不接受自由文本——判断权在 agent 自己的提示词里，不在这里。",
    ]
)


@dataclass
class CommandResult:
    """一次入站消息的处理结果。"""

    handled: bool
    command: str = ""
    reply: str = ""
    reason: str = ""
    admin: bool = False
    silent: bool = False
    """True = 故意不回（比如群里没在白名单里的人说话，不打扰大家）。"""

    data: dict[str, Any] = field(default_factory=dict)
    """gateway 的原始返回（``/run`` ``/archive`` 用它带回 request_id / queued）。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "command": self.command,
            "reply": self.reply,
            "reason": self.reason,
            "admin": self.admin,
            "silent": self.silent,
            "data": self.data,
        }


@runtime_checkable
class AgentGateway(Protocol):
    """命令路由需要的 agent 侧能力（由 :class:`~yuque_agent.qqbot.service.QQBotService` 实现）。"""

    def status(self) -> dict[str, Any]: ...

    def pending_notices(self) -> int: ...

    def request_run(
        self,
        *,
        archive: bool,
        requested_by: str,
        reply_target: Any = None,
    ) -> dict[str, Any]: ...


class CommandRouter:
    """把一条入站消息变成一个动作 + 一句回复。"""

    def __init__(
        self,
        *,
        config: QQBotConfig,
        gateway: AgentGateway,
        log: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.gateway = gateway
        self.log = log
        self._clock = clock
        self._last_admin_call: dict[str, float] = {}

    # -- 入口 -------------------------------------------------------------
    def dispatch(self, msg: InboundMessage) -> CommandResult:
        # 权限档位：个人名单（allow/admins）与群名单（user_groups/admin_groups）各自独立生效
        role = self.config.role_of(msg.sender_id, msg.group_openid)
        admin = role == ROLE_ADMIN
        spec, args = _parse(msg.text)

        # ``/whoami`` 是**唯一**在白名单之前放行的命令：它只回你自己的 openid，
        # 是「怎么把自己加进去」的引导。没有它，第一次用的人会陷入死循环——
        # 机器人跟他说「找管理员加白名单」，而管理员正是他自己，他却拿不到自己的 openid。
        if spec is not None and spec.name == "whoami":
            self._log(f"[qqbot:cmd] whoami ← {msg.sender_id or '?'}（免白名单）")
            return CommandResult(
                handled=True,
                command="whoami",
                reply=whoami_text(msg, self.config),
                admin=admin,
            )

        if not role:
            reason = f"不在白名单（sender={msg.sender_id or '?'} group={msg.group_openid or '-'}）"
            self._log(f"[qqbot:cmd] 拒绝：{reason}")
            if msg.kind == "group":
                # 群里被陌生人 @ 到不打扰大家；要看自己的 id 就显式发 /whoami
                return CommandResult(handled=False, reason=reason, admin=admin, silent=True)
            return CommandResult(
                handled=False,
                reason=reason,
                admin=admin,
                reply=(
                    "你不在这个机器人的白名单里。\n"
                    f"你的 openid：{msg.sender_id or '(拿不到)'}\n"
                    "加白名单有三种方式：\n"
                    "  · 私聊：把 openid 填进 inbound.allow（要管理员再加 inbound.admins）\n"
                    "  · 想整群放开：把群 openid 填进 inbound.user_groups（或 admin_groups）\n"
                    "发 /whoami 可以随时再看一次自己的 id 和当前身份。"
                ),
            )

        if spec is None:
            if not msg.text:
                return CommandResult(handled=False, reason="空消息", admin=admin, silent=True)
            return CommandResult(
                handled=False,
                reason="不是命令",
                admin=admin,
                reply="我只认命令。\n\n" + HELP_TEXT,
            )

        if spec.admin_only and not admin:
            self._log(f"[qqbot:cmd] {msg.sender_id} 想跑 /{spec.name}，但他不是管理员")
            return CommandResult(
                handled=False,
                command=spec.name,
                reason="非管理员",
                admin=False,
                reply=f"/{spec.name} 是写操作，只有管理员能用。",
            )

        if spec.admin_only:
            wait = self._rate_limit_wait(msg.sender_id)
            if wait > 0:
                return CommandResult(
                    handled=False,
                    command=spec.name,
                    reason="被限流",
                    admin=True,
                    reply=f"刚跑过，{wait:.0f} 秒后再来（同一个人的写操作要限流）。",
                )

        reply, data = self._execute(spec, args, msg)
        if spec.admin_only:
            self._last_admin_call[msg.sender_id] = self._clock()
        self._log(f"[qqbot:cmd] {msg.sender_id} → /{spec.name}")
        return CommandResult(handled=True, command=spec.name, reply=reply, admin=admin, data=data)

    # -- 各命令 -----------------------------------------------------------
    def _execute(
        self, spec: CommandSpec, args: str, msg: InboundMessage
    ) -> tuple[str, dict[str, Any]]:
        """返回 ``(回给用户的话, gateway 的原始返回)``。

        ``/run`` ``/archive`` 的原始返回里带着 ``request_id`` / ``queued``，
        服务层要用它把「回执 + 后续分段播报」挂到同一个发送器上。
        """
        if spec.name == "help":
            return HELP_TEXT, {}
        if spec.name == "status":
            return render_status(self.gateway.status()), {}
        if spec.name == "pending":
            count = self.gateway.pending_notices()
            if count <= 0:
                return "通知都投递出去了，pending 是空的。", {}
            return f"还有 {count} 条通知在 outbox/notify/pending/ 里等着投递。", {}
        if spec.name == "run":
            payload = self.gateway.request_run(
                archive=False, requested_by=msg.sender_id, reply_target=msg.reply_target
            )
            return _request_text(payload), payload
        if spec.name == "archive":
            payload = self.gateway.request_run(
                archive=True, requested_by=msg.sender_id, reply_target=msg.reply_target
            )
            return _request_text(payload), payload
        # pragma: no cover - 命令表与分支一一对应
        return f"命令 /{spec.name} 还没实现。", {}

    # -- 内部 -------------------------------------------------------------
    def _rate_limit_wait(self, sender: str) -> float:
        limit = max(0, int(self.config.inbound_rate_limit))
        if limit <= 0:
            return 0.0
        last = self._last_admin_call.get(sender)
        if last is None:
            return 0.0
        return max(0.0, limit - (self._clock() - last))

    def _log(self, text: str) -> None:
        if self.log is not None:
            self.log(text)


def whoami_text(msg: InboundMessage, config: QQBotConfig | None = None) -> str:
    """``/whoami`` 的回复：把**你自己的** id 打出来，并给一段可直接粘的配置。

    只暴露调用者自己的身份（per-bot openid 本来就只对他自己有意义），所以可以在
    白名单之外安全地回。
    """
    lines: list[str] = []
    if config is not None:
        role = config.role_of(msg.sender_id, msg.group_openid)
        label = {ROLE_ADMIN: "管理员", ROLE_USER: "用户"}.get(role, "不在白名单（默认拒绝）")
        lines.append(f"你当前的身份：{label}")
        reason = config.explain_role(msg.sender_id, msg.group_openid)
        if reason:
            lines.append("  依据：" + "；".join(reason))
        lines.append("")
    if msg.kind == "group":
        lines.append("这个是群里的身份（只对这一个机器人有效）：")
        lines.append(f"  群 openid   ：{msg.group_openid or '(拿不到)'}")
        lines.append(f"  群内成员 id ：{msg.sender_id or '(拿不到)'}")
        lines.append("")
        lines.append("要让机器人在这个群里能用命令，两种配法（改完重启服务）：")
        lines.append("  ① 整群放开（群里谁发言都算这个身份，不用逐个登记）：")
        lines.append(f'     "user_groups":  ["{msg.group_openid}"],   ← 普通用户')
        lines.append(
            f'     "admin_groups": ["{msg.group_openid}"],   ← 管理员（能用 /run /archive，慎用）'
        )
        lines.append("  ② 只放某个人：把下面的「群内成员 id」填进 allow/admins：")
        lines.append(f'     "allow":  ["{msg.sender_id}"],')
        lines.append(f'     "admins": ["{msg.sender_id}"]')
        lines.append("")
        lines.append("注意：群内成员 id 和私聊的 openid **不是同一个**，别混用。")
    else:
        lines.append("这是你的身份（只对这一个机器人有效）：")
        lines.append(f"  user_openid：{msg.sender_id or '(拿不到)'}")
        lines.append("")
        lines.append(
            "把它填进 qqbot.json 的 inbound.allow 就能用命令（要跑 /run /archive 再加进 admins），"
        )
        lines.append("然后 `systemctl restart yuque-agent`：")
        lines.append(f'  "allow":  ["{msg.sender_id}"],')
        lines.append(f'  "admins": ["{msg.sender_id}"]')
    return "\n".join(lines)


def _parse(text: str) -> tuple[CommandSpec | None, str]:
    """``/status`` / ``状态`` → ``(CommandSpec, 参数)``；认不出来返回 ``(None, "")``。"""
    raw = (text or "").strip()
    if not raw:
        return None, ""
    match = _SLASH_RE.match(raw)
    if match:
        word, _, rest = match.group(1).partition(" ")
        spec = _COMMAND_INDEX.get(word.lower()) or _COMMAND_INDEX.get(word)
        return (spec, rest.strip()) if spec is not None else (None, "")
    head, _, rest = raw.partition(" ")
    spec = _COMMAND_INDEX.get(head.lower()) or _COMMAND_INDEX.get(head)
    if spec is None:
        return None, ""
    return spec, rest.strip()


def _request_text(payload: dict[str, Any]) -> str:
    if payload.get("queued"):
        return str(payload.get("message") or "已经排进队列，跑完我会把结果发给你。")
    return str(payload.get("message") or "现在不方便跑，稍后再试。")


def render_status(payload: dict[str, Any]) -> str:
    """把 :meth:`AgentGateway.status` 的字典渲染成给人看的短消息。"""
    lines: list[str] = []
    repo = payload.get("repo") or "(未知知识库)"
    lines.append(f"知识库：{repo}")
    watching = payload.get("watching")
    if watching is not None:
        lines.append(f"轮询：{'在跑' if watching else '没在跑'}")
    pending = payload.get("pending")
    if pending is not None:
        lines.append(f"待投递通知：{pending} 条")
    last = payload.get("last_run")
    if isinstance(last, dict) and last:
        when = last.get("at") or "?"
        kind = last.get("kind") or "?"
        verdict = last.get("verdict") or "—"
        summary = last.get("summary") or "(没有摘要)"
        lines.append(f"最近一轮：{when} · {kind} · {verdict}")
        lines.append(f"  {summary}")
        if last.get("run_id"):
            lines.append(f"  run: {last['run_id']}")
    else:
        lines.append("最近一轮：还没有跑过")
    if payload.get("inbound") is not None:
        lines.append(f"QQ 入站：{'开' if payload['inbound'] else '关'}")
    return "\n".join(lines)


__all__ = [
    "COMMANDS",
    "HELP_TEXT",
    "AgentGateway",
    "CommandResult",
    "CommandRouter",
    "CommandSpec",
    "render_status",
    "whoami_text",
]
