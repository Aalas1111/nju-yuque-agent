"""QQBot 凭证存取。

约定（与项目里语雀 token 的规矩一致）：**凭证只从环境变量或已有的凭证文件读、
程序只在扫码登录成功后写一次、任何地方都不打印密钥**。

| 来源 | 优先级 | 说明 |
|---|---|---|
| 显式参数（``--app-id`` / ``--app-secret``） | 1 | 临时用，不落盘 |
| 环境变量 ``YQA_QQ_APPID`` / ``YQA_QQ_SECRET`` | 2 | CI / 容器里最常用 |
| 凭证文件 ``~/.yuque/qqbot.json`` | 3 | 扫码登录的落点，权限 ``600`` |

凭证文件形状（键名对齐参考实现 ``qqbot_backend_sdk`` 的 ``Credentials``，
所以这个文件也能直接喂给那个 SDK）::

    {
      "version": 1,
      "accounts": {
        "default": {
          "appId": "102xxxxxx",
          "clientSecret": "……",
          "userOpenid": "……",
          "boundAt": "2026-09-20T10:18:40+08:00",
          "boundVia": "qr"
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import clock
from .protocol import QQBotError

#: 凭证文件默认位置（与 ``~/.yuque/auth.json`` 同一目录，方便一起备份）。
DEFAULT_CREDENTIALS_PATH = Path.home() / ".yuque" / "qqbot.json"

ENV_APP_ID = "YQA_QQ_APPID"
ENV_APP_SECRET = "YQA_QQ_SECRET"
ENV_ACCOUNT = "YQA_QQ_ACCOUNT"
ENV_CREDENTIALS_PATH = "YQA_QQ_CREDENTIALS"

DEFAULT_ACCOUNT = "default"
CREDENTIALS_VERSION = 1


def mask_secret(secret: str) -> str:
    """把密钥变成能安全打进日志/终端的样子。"""
    text = secret or ""
    if not text:
        return "(空)"
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}…{text[-4:]}（{len(text)} 位）"


def credentials_path(explicit: str | Path | None = None) -> Path:
    """凭证文件路径：显式 > ``YQA_QQ_CREDENTIALS`` > ``~/.yuque/qqbot.json``。"""
    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get(ENV_CREDENTIALS_PATH, "")
    if from_env:
        return Path(from_env).expanduser()
    return DEFAULT_CREDENTIALS_PATH


@dataclass
class QQBotAccount:
    """一个机器人账户。``app_secret`` 不进 ``repr``，避免顺手 ``print`` 就泄露。"""

    app_id: str = ""
    app_secret: str = field(default="", repr=False)
    user_openid: str = ""
    account: str = DEFAULT_ACCOUNT
    bound_at: str = ""
    bound_via: str = ""
    env: str = "production"
    markdown_support: bool = False

    @property
    def complete(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @property
    def masked_secret(self) -> str:
        return mask_secret(self.app_secret)

    def to_dict(self, *, include_secret: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "appId": self.app_id,
            "userOpenid": self.user_openid,
            "boundAt": self.bound_at,
            "boundVia": self.bound_via,
        }
        if include_secret:
            payload["clientSecret"] = self.app_secret
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, account: str = DEFAULT_ACCOUNT) -> QQBotAccount:
        """读一份账户配置。``clientSecret`` 与 ``appSecret`` 都认（兼容两套写法）。"""
        return cls(
            app_id=str(data.get("appId") or data.get("app_id") or ""),
            app_secret=str(
                data.get("clientSecret") or data.get("appSecret") or data.get("app_secret") or ""
            ),
            user_openid=str(data.get("userOpenid") or data.get("user_openid") or ""),
            account=str(data.get("account") or account),
            bound_at=str(data.get("boundAt") or data.get("bound_at") or ""),
            bound_via=str(data.get("boundVia") or data.get("bound_via") or ""),
            env=str(data.get("env") or "production"),
            markdown_support=bool(data.get("markdownSupport") or False),
        )

    def describe(self) -> str:
        """给人看的一行摘要，**不含密钥明文**。"""
        if not self.complete:
            return f"账户 {self.account}：未绑定（缺 AppID / AppSecret）"
        bits = [f"账户 {self.account}", f"AppID {self.app_id}", f"AppSecret {self.masked_secret}"]
        if self.user_openid:
            bits.append(f"绑定者 {self.user_openid}")
        if self.bound_at:
            bits.append(f"绑定于 {self.bound_at}")
        if self.bound_via:
            bits.append(f"来源 {self.bound_via}")
        return " · ".join(bits)


class CredentialStore:
    """``~/.yuque/qqbot.json`` 的读写。写是**原子 + 0600**，失败不会留下半个文件。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = credentials_path(path)
        self._lock = threading.Lock()

    # -- 读 ---------------------------------------------------------------
    def load(self) -> dict[str, QQBotAccount]:
        """读出全部账户；文件不存在 / 坏掉都返回空 dict（不抛，调用方给友好提示）。"""
        with self._lock:
            payload = self._read_raw()
        accounts = payload.get("accounts")
        if not isinstance(accounts, dict):
            # 兼容「文件里直接就是一个账户」的老写法
            if payload.get("appId"):
                return {DEFAULT_ACCOUNT: QQBotAccount.from_dict(payload)}
            return {}
        out: dict[str, QQBotAccount] = {}
        for name, entry in accounts.items():
            if isinstance(entry, dict):
                out[str(name)] = QQBotAccount.from_dict(entry, account=str(name))
        return out

    def get(self, account: str = DEFAULT_ACCOUNT) -> QQBotAccount | None:
        return self.load().get(account)

    # -- 写 ---------------------------------------------------------------
    def save(self, account: QQBotAccount) -> Path:
        """写入/更新一个账户。返回凭证文件路径。"""
        if not account.complete:
            raise QQBotError("拒绝写入不完整的凭证（AppID 与 AppSecret 都必须有）")
        with self._lock:
            payload = self._read_raw()
            accounts = payload.get("accounts")
            if not isinstance(accounts, dict):
                accounts = {}
            accounts[account.account or DEFAULT_ACCOUNT] = account.to_dict()
            payload = {"version": CREDENTIALS_VERSION, "accounts": accounts}
            # 只给**新建**的目录 700；已存在的目录（比如 ~/.yuque）不动它
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._chmod_file(tmp)
            tmp.replace(self.path)
            self._chmod_file(self.path)
        return self.path

    def delete(self, account: str = DEFAULT_ACCOUNT) -> bool:
        """删掉一个账户；删掉最后一个账户时连文件一起删。返回是否真的删了东西。"""
        with self._lock:
            payload = self._read_raw()
            accounts = payload.get("accounts")
            if not isinstance(accounts, dict) or account not in accounts:
                return False
            accounts.pop(account, None)
            if not accounts:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                return True
            payload["accounts"] = accounts
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._chmod_file(tmp)
            tmp.replace(self.path)
        return True

    def accounts(self) -> list[str]:
        return sorted(self.load())

    # -- 内部 -------------------------------------------------------------
    def _read_raw(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _chmod_file(self, path: Path) -> None:
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
        except OSError:  # pragma: no cover
            pass


def resolve_account(
    *,
    account: str = DEFAULT_ACCOUNT,
    app_id: str = "",
    app_secret: str = "",
    store: CredentialStore | None = None,
    env: dict[str, str] | None = None,
) -> QQBotAccount:
    """按「显式 > 环境变量 > 凭证文件」解析出一个账户。无凭证时返回空账户。

    注意：这里**不抛异常**——「还没绑定」是正常状态，由调用方决定怎么提示。
    """
    environ = env if env is not None else os.environ
    store = store or CredentialStore()

    resolved = QQBotAccount(account=account)
    file_account = store.get(account)
    if file_account is not None:
        resolved = file_account

    env_id = environ.get(ENV_APP_ID, "")
    env_secret = environ.get(ENV_APP_SECRET, "")
    if env_id or env_secret:
        resolved.app_id = env_id or resolved.app_id
        resolved.app_secret = env_secret or resolved.app_secret
        resolved.bound_via = resolved.bound_via or "env"
    if app_id:
        resolved.app_id = app_id
        resolved.bound_via = "explicit"
    if app_secret:
        resolved.app_secret = app_secret
        resolved.bound_via = "explicit"
    resolved.account = account
    return resolved


def account_from_bind(
    *,
    app_id: str,
    app_secret: str,
    user_openid: str = "",
    account: str = DEFAULT_ACCOUNT,
    env: str = "production",
    source: str = "qr",
    markdown_support: bool = False,
    now: datetime | None = None,
) -> QQBotAccount:
    """把扫码绑定结果组装成待落盘的账户。"""
    stamp = (now or clock.now()).isoformat(timespec="seconds")
    return QQBotAccount(
        app_id=app_id,
        app_secret=app_secret,
        user_openid=user_openid,
        account=account,
        bound_at=stamp,
        bound_via=source,
        env=env,
        markdown_support=markdown_support,
    )


__all__ = [
    "CREDENTIALS_VERSION",
    "DEFAULT_ACCOUNT",
    "DEFAULT_CREDENTIALS_PATH",
    "ENV_ACCOUNT",
    "ENV_APP_ID",
    "ENV_APP_SECRET",
    "ENV_CREDENTIALS_PATH",
    "CredentialStore",
    "QQBotAccount",
    "account_from_bind",
    "credentials_path",
    "mask_secret",
    "resolve_account",
]
