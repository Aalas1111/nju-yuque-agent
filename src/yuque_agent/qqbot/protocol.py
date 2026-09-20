"""QQ 开放平台协议层——**同步移植版**。

参考实现：[`qqbot-connector-python`](https://git.nju.edu.cn/qqbot-backend-sdk/qqbot-connector-python)
（PyPI 名 ``qqbot-backend-sdk``）里的 ``qqbot_backend_sdk``。那个包是 ``asyncio`` 的，
而本项目（yuque-agent）整体是**同步**的（httpx sync + typer + 常驻轮询），
所以这里按同一套协议、同一批端点**重写为同步实现**，不引入 asyncio：

| 参考实现 | 本模块 | 作用 |
|---|---|---|
| ``auth/qr_session.py`` | :func:`generate_bind_key` / :meth:`QQBotProtocol.create_bind_task` / :meth:`~QQBotProtocol.poll_bind_result` / :func:`decrypt_secret` / :func:`build_connect_url` | 扫码绑定底层接口 |
| ``auth/token.py`` | :meth:`QQBotProtocol.get_access_token` | ``bots.qq.com`` 取 access_token |
| ``api/client.py`` | :meth:`QQBotProtocol.api_request` | ``api.sgroup.qq.com`` REST（逃生舱） |

这一层**只做协议**：不读配置、不落盘、不渲染二维码、不打印任何东西。
所有副作用（存凭证、画二维码、发消息）都在上层模块，方便离线测试。

扫码绑定的完整流程（``q.qq.com`` 官方接口）::

    1. 本地生成 32 字节随机 key（base64）
    2. POST /lite/create_bind_task  {"key": <key>}            → task_id
    3. 用户手机 QQ 打开 connect.html?task_id=<task_id> 扫码
    4. POST /lite/poll_bind_result  {"task_id": <task_id>}   → status
         status=1 待扫码 / 2 已完成 / 3 已过期
    5. status=2 时用本地 key 解密 bot_encrypt_secret（AES-256-GCM）→ 明文 AppSecret
"""

from __future__ import annotations

import base64
import enum
import json
import os
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

# ---------------------------------------------------------------- 常量

#: 正式 / 测试环境的主机名。测试环境只在 QQ 侧灰度时才有，一般用 production。
QQ_HOSTS = {"production": "q.qq.com", "test": "test.q.qq.com"}

#: 机器人 REST 接口（发消息 / 网关地址）。
DEFAULT_API_BASE = "https://api.sgroup.qq.com"

#: 取 access_token 的接口。
DEFAULT_TOKEN_BASE = "https://bots.qq.com"
TOKEN_PATH = "/app/getAppAccessToken"

DEFAULT_TIMEOUT = 10.0
DEFAULT_USER_AGENT = "yuque-agent/qqbot (sync port of qqbot-backend-sdk)"

#: 媒体 / 流式消息的类型编号，与开放平台一致（参考实现 ``types.py`` 的 ``MsgType``）。
MSG_TYPE_TEXT = 0
MSG_TYPE_MARKDOWN = 2
MSG_TYPE_ARK = 3
MSG_TYPE_EMBED = 4
MSG_TYPE_MEDIA = 7

#: 入站消息在 REST 路径上的 scope。
SCOPE_C2C = "c2c"
SCOPE_GROUP = "group"


class QQBotError(RuntimeError):
    """协议层错误。消息面向**人**（会直接打给用户看），不面向机器解析。"""


class ApiError(QQBotError):
    """REST 调用失败（带 HTTP 状态码与路径）。"""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        path: str = "",
        biz_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.path = path
        self.biz_code = biz_code


class BindStatus(enum.IntEnum):
    """``poll_bind_result`` 的状态码（与官方一致）。"""

    NONE = 0
    PENDING = 1
    COMPLETED = 2
    EXPIRED = 3


def bind_status_name(status: int) -> str:
    try:
        return BindStatus(int(status)).name.lower()
    except ValueError:
        return f"unknown({status})"


def get_qqbot_host(env: str = "production") -> str:
    """``production`` → ``q.qq.com``；``test`` → ``test.q.qq.com``。"""
    return QQ_HOSTS.get(env, QQ_HOSTS["production"])


def generate_bind_key() -> str:
    """绑定任务的随机 key：32 字节 → base64。**必须留在本地**，它是解密 AppSecret 的钥匙。"""
    return base64.b64encode(os.urandom(32)).decode("ascii")


def build_connect_url(task_id: str, source: str = "") -> str:
    """把 ``task_id`` 变成用户手机上要打开的扫码绑定页地址。"""
    return (
        "https://q.qq.com/qqbot/openclaw/connect.html"
        f"?task_id={urllib.parse.quote(str(task_id))}"
        f"&source={urllib.parse.quote(source or '')}&_wv=2"
    )


def decrypt_secret(encrypted_base64: str, key_base64: str) -> str:
    """解密 ``bot_encrypt_secret``，得到明文 AppSecret。

    算法（与官方 / 参考实现一致）::

        key        = createBindTask 时本地生成的 base64 key（解码后 32 字节）
        ciphertext = IV(12B) + data + AuthTag(16B)，整体是 base64
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底提示
        raise QQBotError(
            "扫码绑定需要 cryptography（AES-256-GCM 解密 AppSecret）：pip install cryptography"
        ) from exc

    try:
        key = base64.b64decode(key_base64, validate=True)
        data = base64.b64decode(encrypted_base64, validate=True)
    except Exception as exc:
        raise QQBotError(f"AppSecret 密文不是合法 base64：{exc}") from exc

    if len(key) != 32:
        raise QQBotError(f"绑定 key 必须是 32 字节，实际 {len(key)} 字节")
    if len(data) <= 12 + 16:
        raise QQBotError("AppSecret 密文长度不足（至少 IV 12 字节 + AuthTag 16 字节）")

    iv, ciphertext, tag = data[:12], data[12:-16], data[-16:]
    try:
        plain = AESGCM(key).decrypt(iv, ciphertext + tag, None)
    except Exception as exc:
        raise QQBotError(f"AppSecret 解密失败（key 不匹配或密文损坏）：{exc}") from exc
    return plain.decode("utf-8")


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class BindTask:
    """一次绑定任务。``key`` 只应留在内存里。"""

    task_id: str
    key: str = field(repr=False)
    connect_url: str = ""


@dataclass(frozen=True)
class BindResult:
    """一次 ``poll_bind_result`` 的结果。"""

    status: int
    bot_app_id: str = ""
    bot_encrypt_secret: str = field(default="", repr=False)
    user_openid: str = ""

    @property
    def status_name(self) -> str:
        return bind_status_name(self.status)

    @property
    def completed(self) -> bool:
        return self.status == int(BindStatus.COMPLETED)

    @property
    def expired(self) -> bool:
        return self.status == int(BindStatus.EXPIRED)


@dataclass(frozen=True)
class TokenInfo:
    """access_token 及其到期时间。"""

    access_token: str = field(repr=False)
    expires_in: int = 7200
    obtained_at: float = 0.0

    @property
    def expires_at(self) -> float:
        return self.obtained_at + self.expires_in


# ---------------------------------------------------------------- 传输层


@runtime_checkable
class HttpResponse(Protocol):
    """httpx 响应里我们真正用到的部分（测试用假响应很好造）。"""

    status_code: int
    text: str

    @property
    def headers(self) -> Mapping[str, str]: ...

    def json(self) -> Any: ...


@runtime_checkable
class Transport(Protocol):
    """最小 HTTP 传输接口。测试注入假实现即可完全离线。"""

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResponse: ...

    def close(self) -> None: ...


class HttpxTransport:
    """默认传输：一个长生命周期的 ``httpx.Client``（连接池复用）。"""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._timeout = timeout
        self._client = client or httpx.Client(follow_redirects=True)
        self._owns_client = client is None

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> HttpResponse:
        merged = {"User-Agent": self._user_agent, "Content-Type": "application/json"}
        if headers:
            merged.update(headers)
        return self._client.request(
            method.upper(),
            url,
            json=json,
            headers=merged,
            timeout=httpx.Timeout(timeout),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


# ---------------------------------------------------------------- 协议实现


class QQBotProtocol:
    """QQ 开放平台协议客户端（同步）。

    只依赖一个满足 :class:`Transport` 的对象；默认用 httpx。
    """

    def __init__(
        self,
        *,
        env: str = "production",
        transport: Transport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        api_base: str = DEFAULT_API_BASE,
        token_base: str = DEFAULT_TOKEN_BASE,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.env = env
        self.timeout = timeout
        self.api_base = api_base.rstrip("/")
        self.token_base = token_base.rstrip("/")
        self._transport: Transport = transport or HttpxTransport(
            user_agent=user_agent, timeout=timeout
        )
        self._owns_transport = transport is None

    # -- 生命周期 ---------------------------------------------------------
    def close(self) -> None:
        if self._owns_transport:
            self._transport.close()

    def __enter__(self) -> QQBotProtocol:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def host(self) -> str:
        return get_qqbot_host(self.env)

    # -- 扫码绑定 ---------------------------------------------------------
    def create_bind_task(self) -> BindTask:
        """创建绑定任务。返回的 ``key`` 必须留着解密 AppSecret。"""
        key = generate_bind_key()
        data = self._post_json(
            f"https://{self.host}/lite/create_bind_task",
            {"key": key},
            what="create_bind_task",
        )
        payload = data.get("data") or {}
        task_id = str(payload.get("task_id") or "")
        if not task_id:
            raise QQBotError("create_bind_task 没有返回 task_id")
        return BindTask(task_id=task_id, key=key)

    def poll_bind_result(self, task_id: str) -> BindResult:
        """轮询绑定结果。**不要**在 status=PENDING 时反复打印错误——那只是还没扫。"""
        data = self._post_json(
            f"https://{self.host}/lite/poll_bind_result",
            {"task_id": str(task_id)},
            what="poll_bind_result",
        )
        payload = data.get("data") or {}
        status = payload.get("status")
        try:
            status_int = int(status) if status is not None else int(BindStatus.NONE)
        except (TypeError, ValueError):
            status_int = int(BindStatus.NONE)
        return BindResult(
            status=status_int,
            bot_app_id=str(payload.get("bot_appid") or ""),
            bot_encrypt_secret=str(payload.get("bot_encrypt_secret") or ""),
            user_openid=str(payload.get("openid") or payload.get("user_openid") or ""),
        )

    # -- access_token -----------------------------------------------------
    def get_access_token(self, app_id: str, client_secret: str) -> TokenInfo:
        """``POST bots.qq.com/app/getAppAccessToken``。"""
        if not app_id or not client_secret:
            raise QQBotError("取 access_token 需要 appId 与 clientSecret")
        response = self._transport.request(
            "POST",
            f"{self.token_base}{TOKEN_PATH}",
            json={"appId": app_id, "clientSecret": client_secret},
            timeout=self.timeout,
        )
        data = self._decode(response, what="getAppAccessToken")
        token = data.get("access_token")
        if not token:
            raise QQBotError(f"开放平台没有返回 access_token：{_brief(data)}")
        try:
            expires_in = int(data.get("expires_in") or 7200)
        except (TypeError, ValueError):
            expires_in = 7200
        return TokenInfo(access_token=str(token), expires_in=expires_in, obtained_at=time.time())

    # -- REST -------------------------------------------------------------
    def api_request(
        self,
        token: str,
        method: str,
        path: str,
        body: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """任意开放平台 REST 接口（自动注入 ``Authorization: QQBot <token>``）。"""
        if not path.startswith("/"):
            path = "/" + path
        response = self._transport.request(
            method.upper(),
            f"{self.api_base}{path}",
            json=body,
            headers={"Authorization": f"QQBot {token}"},
            timeout=timeout or self.timeout,
        )
        return self._decode(
            response, what=f"{method.upper()} {path}", path=path, status=response.status_code
        )

    # -- 内部 -------------------------------------------------------------
    def _post_json(self, url: str, body: Any, *, what: str) -> dict[str, Any]:
        response = self._transport.request("POST", url, json=body, timeout=self.timeout)
        data = self._decode(response, what=what, status=response.status_code)
        if not isinstance(data, dict):
            raise QQBotError(f"{what}：响应不是 JSON 对象")
        retcode = data.get("retcode", 0)
        if retcode not in (0, None):
            raise QQBotError(f"{what} 失败：retcode={retcode} msg={data.get('msg') or '(无)'}")
        return data

    @staticmethod
    def _decode(
        response: HttpResponse, *, what: str, path: str = "", status: int | None = None
    ) -> Any:
        status = status if status is not None else response.status_code
        text = (response.text or "")[:200]
        if status >= 400:
            hint = {
                401: "鉴权失败（AppID/AppSecret 是否正确？）",
                403: "没有权限（机器人可能未上线或权限未申请）",
                404: "接口不存在",
                429: "请求过于频繁，被限流了",
            }.get(status, f"HTTP {status}")
            raise ApiError(f"{what} 调用失败：{hint}；{text}", status=status, path=path)
        if text.lstrip().startswith("<"):
            raise ApiError(
                f"{what} 返回了 HTML 而不是 JSON（可能是临时故障）：{text}",
                status=status,
                path=path,
            )
        try:
            return json.loads(response.text or "null")
        except ValueError as exc:
            raise ApiError(f"{what} 响应不是合法 JSON：{exc}", status=status, path=path) from exc


def _brief(data: Any, limit: int = 200) -> str:
    text = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
    return text[:limit]


__all__ = [
    "ApiError",
    "BindResult",
    "BindStatus",
    "BindTask",
    "DEFAULT_API_BASE",
    "DEFAULT_TIMEOUT",
    "DEFAULT_TOKEN_BASE",
    "DEFAULT_USER_AGENT",
    "HttpxTransport",
    "HttpResponse",
    "MSG_TYPE_MARKDOWN",
    "MSG_TYPE_TEXT",
    "QQBotError",
    "QQBotProtocol",
    "SCOPE_C2C",
    "SCOPE_GROUP",
    "TOKEN_PATH",
    "TokenInfo",
    "Transport",
    "bind_status_name",
    "build_connect_url",
    "decrypt_secret",
    "generate_bind_key",
    "get_qqbot_host",
]
