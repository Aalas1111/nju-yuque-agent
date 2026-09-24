"""QQBot 接入配置：**通知投给谁** + **谁能让 agent 干活**。

文件放在工作区里（与 ``state.json``、``outbox/`` 同级，天然不入库）::

    <workspace>/<repo_slug>/qqbot.json

```jsonc
{
  "version": 1,
  "source": "yuque-agent",
  "notify": {
    "unmapped": "default",                 // default = 发给 default_target；skip = 挪去 unrouted/
    "default_target": { "scope": "group", "targetId": "群 openid" },
    "members": {
      "张三": { "scope": "c2c", "targetId": "用户 openid" }
    }
  },
  "inbound": {
    "enabled": true,
    "users":  ["用户 openid"],             // 用户权限：借用教室、查帮助等
    "admins": ["管理员 openid"],           // 管理员权限：跑轮询、归档、查后端状态等
    "user_groups": ["群 openid"],          // 用户群：群里任何人发言都算用户
    "admin_groups": ["群 openid"],         // 管理员群：群里任何人发言都算管理员
    "rate_limit_seconds": 30
  }
}
```

权限模型：
- 两层权限：user（用户）和 admin（管理员）
- 管理员自动拥有用户权限
- 一个 OpenID 只能在一个层级（users 和 admins 不能重复）
- 默认拒绝：users 和 admins 都空 = 谁都不能用
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .client import Target

CONFIG_VERSION = 1
DEFAULT_CONFIG_NAME = "qqbot.json"
ENV_CONFIG_PATH = "YQA_QQ_CONFIG"

#: 权限档位（``QQBotConfig.role_of`` 的返回值）。
ROLE_ADMIN = "admin"
ROLE_USER = "user"

#: 走不动了：默认拒绝。
UNMAPPED_DEFAULT = "default"
UNMAPPED_SKIP = "skip"


@dataclass(frozen=True)
class NotifyTarget:
    """一条通知该发到哪。"""

    scope: str
    target_id: str
    note: str = ""

    @property
    def target(self) -> Target:
        return Target(scope=self.scope, target_id=self.target_id)

    def to_str(self) -> str:
        return f"{self.scope}:{self.target_id}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"scope": self.scope, "targetId": self.target_id}
        if self.note:
            payload["note"] = self.note
        return payload

    @classmethod
    def from_dict(cls, data: Any) -> NotifyTarget | None:
        if not isinstance(data, dict):
            return None
        scope = str(data.get("scope") or "c2c").strip().lower()
        target_id = str(
            data.get("targetId") or data.get("target_id") or data.get("openid") or ""
        ).strip()
        if not target_id:
            return None
        if scope not in ("c2c", "group"):
            return None
        return cls(scope=scope, target_id=target_id, note=str(data.get("note") or ""))


@dataclass
class QQBotConfig:
    """``qqbot.json`` 的内存形态。"""

    version: int = CONFIG_VERSION
    source: str = "yuque-agent"
    notify_unmapped: str = UNMAPPED_DEFAULT
    notify_default: NotifyTarget | None = None
    members: dict[str, NotifyTarget] = field(default_factory=dict)
    inbound_enabled: bool = True
    inbound_users: tuple[str, ...] = ()
    """个人用户：这些 openid 拥有用户权限（借用教室、查帮助等）。"""

    inbound_admins: tuple[str, ...] = ()
    """个人管理员：这些 openid 拥有管理员权限（跑轮询、归档等），自动拥有用户权限。"""

    inbound_user_groups: tuple[str, ...] = ()
    """**用户群**：这些群里的任何人发言都算「用户」——不用逐个登记 openid。"""

    inbound_admin_groups: tuple[str, ...] = ()
    """**管理员群**：这些群里的任何人发言都算「管理员」。

    ⚠️ 高风险：群成员由群管理员控制，而 ``/archive`` 能删文档、移目录。
    只把它用在你自己完全掌控的小群里；更稳的做法是用 ``admins`` 逐个授权。
    """

    inbound_rate_limit: int = 30
    path: Path | None = None

    # -- 读 ---------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> QQBotConfig:
        """读配置；文件不存在 / 坏掉 → 返回默认配置（默认拒绝一切入站）。"""
        config_path = Path(path) if path else None
        if config_path is None or not config_path.exists():
            return cls(path=config_path)
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path=config_path)
        if not isinstance(data, dict):
            return cls(path=config_path)

        notify = data.get("notify") if isinstance(data.get("notify"), dict) else {}
        inbound = data.get("inbound") if isinstance(data.get("inbound"), dict) else {}
        members: dict[str, NotifyTarget] = {}
        raw_users = notify.get("members") if isinstance(notify.get("members"), dict) else {}
        for name, entry in raw_users.items():
            parsed = NotifyTarget.from_dict(entry)
            if parsed is not None:
                members[str(name).strip()] = parsed

        unmapped = str(notify.get("unmapped") or UNMAPPED_DEFAULT).strip().lower()
        if unmapped not in (UNMAPPED_DEFAULT, UNMAPPED_SKIP):
            unmapped = UNMAPPED_DEFAULT
        try:
            rate_limit = int(inbound.get("rate_limit_seconds") or 30)
        except (TypeError, ValueError):
            rate_limit = 30

        # 兼容旧键 allow → users
        users_val = _str_tuple(inbound.get("users")) or _str_tuple(inbound.get("allow"))

        return cls(
            version=int(data.get("version") or CONFIG_VERSION),
            source=str(data.get("source") or "yuque-agent"),
            notify_unmapped=unmapped,
            notify_default=NotifyTarget.from_dict(notify.get("default_target")),
            members=members,
            inbound_enabled=bool(inbound.get("enabled", True)),
            inbound_users=users_val,
            inbound_admins=_str_tuple(inbound.get("admins")),
            inbound_user_groups=_str_tuple(inbound.get("user_groups"))
            or _str_tuple(inbound.get("groups")),
            inbound_admin_groups=_str_tuple(inbound.get("admin_groups")),
            inbound_rate_limit=max(0, rate_limit),
            path=config_path,
        )

    # -- 写 ---------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CONFIG_VERSION,
            "source": self.source,
            "notify": {
                "unmapped": self.notify_unmapped,
                "default_target": self.notify_default.to_dict() if self.notify_default else None,
                "members": {
                    name: target.to_dict() for name, target in sorted(self.members.items())
                },
            },
            "inbound": {
                "enabled": self.inbound_enabled,
                "users": list(self.inbound_users),
                "admins": list(self.inbound_admins),
                "user_groups": list(self.inbound_user_groups),
                "admin_groups": list(self.inbound_admin_groups),
                "rate_limit_seconds": self.inbound_rate_limit,
            },
        }

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("没有配置路径可写")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
        self.path = target
        return target

    # -- 查询 -------------------------------------------------------------
    def resolve_member(self, name: str) -> tuple[NotifyTarget | None, str]:
        """语雀侧人名 → QQ 目标。返回 ``(目标, 依据)``，依据取值见下。"""
        wanted = (name or "").strip()
        if wanted and wanted in self.members:
            return self.members[wanted], "member"
        if wanted:
            lowered = wanted.lower()
            for key, target in self.members.items():
                if key.lower() == lowered:
                    return target, "member"
        if self.notify_unmapped == UNMAPPED_SKIP:
            return None, "skip"
        if self.notify_default is not None:
            return self.notify_default, "default"
        return None, "unmapped"

    def role_of(self, sender_id: str, group_openid: str = "") -> str:
        """判定这条消息的权限档位：``"admin"`` / ``"user"`` / ``""``（默认拒绝）。

        权限模型：
        - 管理员（admins / admin_groups）自动拥有用户权限
        - 一个 OpenID 只能在一个层级
        - 四个来源各自独立生效，满足其一即可::

            群消息：  群 ∈ admin_groups 或 发言人 ∈ admins  → admin
                     群 ∈ user_groups  或 发言人 ∈ users   → user
            私聊：   发言人 ∈ admins → admin ／ ∈ users → user
        """
        if not self.inbound_enabled:
            return ""
        group = (group_openid or "").strip()
        person = (sender_id or "").strip()
        if group:
            if group in self.inbound_admin_groups or person in self.inbound_admins:
                return ROLE_ADMIN
            if group in self.inbound_user_groups or person in self.inbound_users:
                return ROLE_USER
            return ""
        if person and person in self.inbound_admins:
            return ROLE_ADMIN
        if person and person in self.inbound_users:
            return ROLE_USER
        return ""

    def explain_role(self, sender_id: str, group_openid: str = "") -> list[str]:
        """给出「为什么你是这个档位」的人话依据（``/whoami`` 用它告诉用户怎么配）。"""
        group = (group_openid or "").strip()
        person = (sender_id or "").strip()
        role = self.role_of(sender_id, group_openid)
        if not role:
            return []
        reason: list[str] = []
        if group:
            if group in self.inbound_admin_groups:
                reason.append(f"群 {group} 配在 inbound.admin_groups 里")
            if group in self.inbound_user_groups and group not in self.inbound_admin_groups:
                reason.append(f"群 {group} 配在 inbound.user_groups 里")
        if person in self.inbound_admins:
            reason.append("你的 openid 配在 inbound.admins 里")
        elif person in self.inbound_users:
            reason.append("你的 openid 配在 inbound.users 里")
        return reason

    def is_allowed(self, sender_id: str, group_openid: str = "") -> bool:
        """这条入站消息有没有资格跟 bot 说话。**默认拒绝**。"""
        return self.role_of(sender_id, group_openid) != ""

    def is_admin(self, sender_id: str, group_openid: str = "") -> bool:
        return self.role_of(sender_id, group_openid) == ROLE_ADMIN

    @property
    def inbound_group_count(self) -> int:
        return len(set(self.inbound_user_groups) | set(self.inbound_admin_groups))

    def problems(self) -> list[str]:
        """配置体检（doctor 用）：返回人话描述的问题列表，空 = 没问题。"""
        issues: list[str] = []
        if not any(
            (
                self.inbound_users,
                self.inbound_admins,
                self.inbound_user_groups,
                self.inbound_admin_groups,
            )
        ):
            issues.append(
                "入站命令对所有人都关着（默认拒绝）：要开放就填 users/admins，或把群加进 user_groups"
            )
        if self.inbound_admin_groups:
            issues.append(
                f"配了 {len(self.inbound_admin_groups)} 个管理员群 → 群里任何人都能跑 "
                "/run /archive（后者能删文档、移目录）。确认群成员可控；更稳的是用 admins 逐个授权"
            )
        overlap_groups = set(self.inbound_user_groups) & set(self.inbound_admin_groups)
        if overlap_groups:
            issues.append(
                "这些群同时在 user_groups 与 admin_groups 里，按管理员处理："
                + "、".join(sorted(overlap_groups))
            )
        # 检查 users 和 admins 是否有重复
        overlap_users = set(self.inbound_users) & set(self.inbound_admins)
        if overlap_users:
            issues.append(
                "这些 openid 同时在 users 与 admins 里（一个 openid 只能在一个层级）："
                + "、".join(sorted(overlap_users))
            )
        if self.notify_unmapped == UNMAPPED_DEFAULT and self.notify_default is None:
            issues.append(
                "notify.unmapped=default 但没有配 default_target → 认不出人的通知会被挪到 unrouted/"
            )
        if not self.members and self.notify_default is None:
            issues.append("notify.members 与 default_target 都空 → 通知无法投递")
        return issues

    def describe(self) -> str:
        parts = [
            f"配置 {self.path or '(未指定)'}",
            f"成员映射 {len(self.members)} 条",
            f"兜底目标 {self.notify_default.to_str() if self.notify_default else '(无)'}",
            f"入站 {'开' if self.inbound_enabled else '关'}",
            f"用户 {len(self.inbound_users)} 人",
            f"管理员 {len(self.inbound_admins)} 人",
            f"用户群 {len(self.inbound_user_groups)} 个",
            f"管理员群 {len(self.inbound_admin_groups)} 个",
        ]
        return " · ".join(parts)


def _str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def default_config_path(settings: Any | None = None) -> Path:
    """``<workspace>/<repo_slug>/qqbot.json``；也认 ``YQA_QQ_CONFIG``。"""
    from_env = os.environ.get(ENV_CONFIG_PATH, "")
    if from_env:
        return Path(from_env).expanduser()
    if settings is not None:
        return Path(settings.root) / DEFAULT_CONFIG_NAME
    return Path(DEFAULT_CONFIG_NAME)


def load_config(settings: Any | None = None, path: str | Path | None = None) -> QQBotConfig:
    return QQBotConfig.load(path if path is not None else default_config_path(settings))


def init_config(settings: Any | None = None, path: str | Path | None = None) -> Path:
    """写一份带注释性说明的空模板（``yqa qq config --init``）。"""
    target = Path(path) if path else default_config_path(settings)
    if target.exists():
        return target
    return QQBotConfig(path=target).save(target)


__all__ = [
    "CONFIG_VERSION",
    "DEFAULT_CONFIG_NAME",
    "ENV_CONFIG_PATH",
    "QQBotConfig",
    "NotifyTarget",
    "ROLE_ADMIN",
    "ROLE_USER",
    "UNMAPPED_DEFAULT",
    "UNMAPPED_SKIP",
    "default_config_path",
    "init_config",
    "load_config",
]
