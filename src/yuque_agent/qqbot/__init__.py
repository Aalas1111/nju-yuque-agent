"""QQBot 接入层。

参考实现：[`qqbot-connector-python`](https://git.nju.edu.cn/qqbot-backend-sdk/qqbot-connector-python)
（``qqbot_backend_sdk``）。本项目按同一套开放平台协议做了**同步移植**，
因为它整体是同步的（httpx + typer + 常驻轮询），引入 asyncio 会让常驻服务的复杂度翻倍。

分成三层，各层都能单独测：

* **协议层** :mod:`.protocol` —— create_bind_task / poll_bind_result / AES-GCM 解密 / access_token / REST
* **登录层** :mod:`.login` :mod:`.login_http` :mod:`.credentials` :mod:`.qr` —— 扫码绑定与凭证落盘
* **业务层** :mod:`.bridge` :mod:`.agent` :mod:`.service` :mod:`.gateway` —— 通知投递 + 入站命令

一句话：**agent 照旧只做感知与留痕，QQ 只是它的一个收发口**。
轮询/归档归核心的常驻进程；本层只做投递 + 命令（见 ``docs/interface.md``）。
"""

from .agent import AgentResult, RouterAgent, register_default_workflows
from .bridge import DeliveryResult, NotifyBridge
from .client import MessageSender, NullSender, QQBotClient, Target
from .config import (
    NotifyTarget,
    QQBotConfig,
    default_config_path,
    init_config,
    load_config,
)
from .credentials import (
    CredentialStore,
    QQBotAccount,
    account_from_bind,
    credentials_path,
    mask_secret,
    resolve_account,
)
from .events import InboundMessage, parse_event
from .login import QrLoginFlow, QrLoginManager, QrLoginResult
from .login_http import LoginHttpServer, serve_login_http
from .protocol import (
    ApiError,
    BindStatus,
    QQBotError,
    QQBotProtocol,
    build_connect_url,
    decrypt_secret,
)
from .qr import has_png_support, has_qr_support, qr_data_url, save_png, support_note, terminal_qr
from .service import QQBotService

__all__ = [
    "AgentResult",
    "ApiError",
    "BindStatus",
    "CredentialStore",
    "RouterAgent",
    "DeliveryResult",
    "InboundMessage",
    "LoginHttpServer",
    "MessageSender",
    "NotifyBridge",
    "NotifyTarget",
    "NullSender",
    "QQBotAccount",
    "QQBotClient",
    "QQBotConfig",
    "QQBotError",
    "QQBotProtocol",
    "QQBotService",
    "QrLoginFlow",
    "QrLoginManager",
    "QrLoginResult",
    "Target",
    "account_from_bind",
    "build_connect_url",
    "credentials_path",
    "decrypt_secret",
    "default_config_path",
    "has_png_support",
    "has_qr_support",
    "init_config",
    "load_config",
    "mask_secret",
    "parse_event",
    "qr_data_url",
    "register_default_workflows",
    "resolve_account",
    "save_png",
    "serve_login_http",
    "support_note",
    "terminal_qr",
]
