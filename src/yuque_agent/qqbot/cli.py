"""``yqa qq …`` 子命令：扫码登录 / 通知投递 / 常驻服务。

```
yqa qq login      # 扫码绑定机器人（终端二维码 / --png / --http）
yqa qq status     # 看绑定状态、配置体检、待投递数量
yqa qq logout     # 删掉本地凭证
yqa qq send       # 手动发一条消息（联调用）
yqa qq notify     # 把 outbox/notify/pending 里的通知投出去
yqa qq config     # 看/初始化 qqbot.json（成员映射 + 入站白名单）
yqa qq doctor     # 依赖 / 凭证 / 配置 / 二维码 / 网关 自检
yqa qq serve      # 常驻：轮询 + 投递通知 + 接 QQ 命令
```
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..config import DEFAULT_MODEL, DEFAULT_REPO, Settings
from ..llm import LLMClient
from ..runner import Runner
from ..watcher import Watcher
from ..yuque import YuqueClient
from .bridge import NotifyBridge
from .client import MessageSender, NullSender, QQBotClient, Target
from .commands import HELP_TEXT
from .config import QQBotConfig, default_config_path, init_config
from .credentials import (
    CredentialStore,
    QQBotAccount,
    account_from_bind,
    credentials_path,
    resolve_account,
)
from .login import QrLoginFlow, QrLoginManager, QrLoginResult
from .login_http import LoginHttpServer
from .protocol import QQBotError, QQBotProtocol
from .qr import has_qr_support, save_png, support_note, terminal_qr
from .service import QQBotService

qq_app = typer.Typer(help="QQBot 接入：扫码登录 / 通知投递 / 常驻服务", no_args_is_help=True)
console = Console()

AccountOpt = Annotated[str, typer.Option("--account", "-a", help="账户名（默认 default）")]
WorkspaceOpt = Annotated[Path, typer.Option("--workspace", "-w", help="工作区目录")]
RepoOpt = Annotated[str, typer.Option("--repo", "-r", help="语雀知识库 namespace")]
CredentialsOpt = Annotated[
    Path | None, typer.Option("--credentials", help="凭证文件（默认 ~/.yuque/qqbot.json）")
]
EnvOpt = Annotated[str, typer.Option("--env", help="开放平台环境：production / test")]


# ---------------------------------------------------------------- 公共


def plain_log(text: str) -> None:
    """原样打印日志。

    **不要用 ``console.print`` 直接打日志**：rich 会把 ``[qqbot:in]`` 当成 markup 标签吃掉，
    日志里最重要的前缀（模块 + 消息方向）就没了。``markup=False`` 才能原样输出。
    """
    console.print(text, markup=False, highlight=False)


def dim_note(text: str) -> None:
    """带动态文本的弱化提示（不能用 f-string 拼 markup）。"""
    console.print(Text(text, style="dim"))


def _settings(
    repo: str,
    workspace: Path,
    *,
    dry_run: bool = False,
    journal: bool = False,
    model: str = DEFAULT_MODEL,
    interval: int = 60,
) -> Settings:
    return Settings.from_env(
        repo=repo,
        workspace=workspace,
        model=model,
        interval=interval,
        dry_run=dry_run,
        journal=journal,
    )


def _store(path: Path | None) -> CredentialStore:
    return CredentialStore(credentials_path(path))


def _config(settings: Settings) -> QQBotConfig:
    return QQBotConfig.load(default_config_path(settings))


def make_sender(
    *,
    account: str = "default",
    credentials: Path | None = None,
    env: str = "production",
    dry_run: bool = False,
    log: Any = None,
    login_if_needed: bool = False,
    source: str = "yuque-agent",
) -> tuple[MessageSender, QQBotProtocol | None]:
    """拿一个发送器（不打印、不 ``sys.exit``，方便被别的命令复用）。

    * 已绑定 → :class:`QQBotClient`；
    * ``dry_run`` → :class:`NullSender`（不需要凭证）；
    * 没绑定且 ``login_if_needed`` → **现场走一遍扫码登录**，成功后继续（见 :func:`auto_login`）；
    * 没绑定且没开自动登录 → 抛 :class:`QQBotError`。
    """
    store = _store(credentials)
    resolved = resolve_account(account=account, store=store)
    if dry_run:
        return NullSender(log=log), None
    if not resolved.complete and login_if_needed:
        resolved = auto_login(
            account=account,
            credentials=credentials,
            env=env,
            source=source,
            log=log,
        )
    if not resolved.complete:
        raise QQBotError(
            "还没有绑定机器人：先跑 `yqa qq login` 扫码，或设 YQA_QQ_APPID / YQA_QQ_SECRET"
        )
    if env:
        resolved.env = env
    protocol = QQBotProtocol(env=resolved.env)
    return QQBotClient(resolved, protocol=protocol, log=log), protocol


def _client(
    *,
    account: str,
    credentials: Path | None,
    env: str,
    dry_run: bool = False,
    log: Any = None,
    login_if_needed: bool = False,
) -> tuple[MessageSender, QQBotProtocol | None]:
    """拿一个发送器：绑定了就是真客户端，``--dry-run`` 就是假发送器。"""
    try:
        return make_sender(
            account=account,
            credentials=credentials,
            env=env,
            dry_run=dry_run,
            log=log,
            login_if_needed=login_if_needed,
        )
    except QQBotError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


# ---------------------------------------------------------------- 自动扫码


def _interactive() -> bool:
    """能不能在终端里出示二维码（stdout 是 tty 才算）。"""
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - 被替换掉的 stdout
        return False


def auto_login(
    *,
    account: str = "default",
    credentials: Path | None = None,
    env: str = "production",
    source: str = "yuque-agent",
    markdown: bool = False,
    timeout: float = 120.0,
    poll: float = 2.0,
    max_refreshes: int = 6,
    log: Any = None,
) -> QQBotAccount:
    """**没有缓存凭证时，现场走一遍扫码登录**，成功后落盘并返回账户。

    这是 ``yqa qq serve`` / ``yqa run --qq`` 的默认行为：第一次启动不用先单独跑
    ``yqa qq login``，扫一下就继续启动；之后启动直接用 ``~/.yuque/qqbot.json`` 里的凭证。

    只在**交互式终端**里做：stdout 不是 tty（systemd / cron / 管道重定向）时直接报错，
    因为二维码必须有人能看见才有意义——那种场景请先在有终端的地方登录一次，
    或者用 ``yqa qq login --png`` / ``--http``，或者直接设 ``YQA_QQ_APPID`` / ``YQA_QQ_SECRET``。
    """
    if not _interactive():
        raise QQBotError(
            "没有缓存凭证，而且当前输出不是终端，没法出示二维码。\n"
            "  ① 先在能看见终端的地方跑一次 `yqa qq login`（凭证在 ~/.yuque/qqbot.json，之后可复制到本机）；\n"
            "  ② 或者 `yqa qq login --png qr.png` / `yqa qq login --http 127.0.0.1:8765`；\n"
            "  ③ 或者直接设 YQA_QQ_APPID / YQA_QQ_SECRET；\n"
            "  ④ 不想让服务自己登录就加 --no-login，让它直接报错。"
        )

    store = _store(credentials)
    protocol = QQBotProtocol(env=env)
    saved: dict[str, Any] = {}

    def on_connected(result: QrLoginResult) -> None:
        bound = account_from_bind(
            app_id=result.app_id,
            app_secret=result.app_secret,
            user_openid=result.user_openid,
            account=account,
            env=env,
            source="qr",
            markdown_support=markdown,
        )
        saved["path"] = store.save(bound)
        saved["account"] = bound

    say = log or plain_log
    say(f"[qqbot] 没有找到 {store.path} 里的凭证 → 先扫码绑定（只需做一次）")
    try:
        result = _login_in_terminal(
            protocol=protocol,
            account=account,
            source=source,
            timeout=timeout,
            poll=poll,
            max_refreshes=max_refreshes,
            png=None,
            no_qr=False,
            no_ansi=False,
            on_connected=on_connected,
            on_status=dim_note,
        )
    finally:
        protocol.close()

    if "account" not in saved:
        reason = result.message if result is not None else "已取消"
        raise QQBotError(f"扫码绑定没有完成（{reason}），服务不启动。")
    bound = saved["account"]
    say(f"[qqbot] ✓ 已绑定 AppID {bound.app_id}，凭证写入 {store.path}")
    return bound


# ---------------------------------------------------------------- login


@qq_app.command("login")
def qq_login(
    account: AccountOpt = "default",
    source: Annotated[
        str, typer.Option("--source", help="来源标识，会写进二维码链接")
    ] = "yuque-agent",
    workspace: WorkspaceOpt = Path("workspace"),
    repo: RepoOpt = DEFAULT_REPO,
    env: EnvOpt = "production",
    timeout: Annotated[
        float, typer.Option("--timeout", help="单张二维码等多久（秒），超时自动刷新")
    ] = 120.0,
    poll: Annotated[float, typer.Option("--poll", help="轮询间隔（秒）")] = 2.0,
    max_refreshes: Annotated[int, typer.Option("--max-refreshes", help="最多刷新几张二维码")] = 6,
    png: Annotated[Path | None, typer.Option("--png", help="同时把二维码存成 PNG")] = None,
    no_qr: Annotated[bool, typer.Option("--no-qr", help="不在终端画二维码，只打印链接")] = False,
    no_ansi: Annotated[
        bool, typer.Option("--no-ansi", help="二维码不用 ANSI 配色（老终端）")
    ] = False,
    markdown: Annotated[
        bool, typer.Option("--markdown/--no-markdown", help="这个机器人有没有 Markdown 权限")
    ] = False,
    test: Annotated[bool, typer.Option("--test", help="绑定成功后立刻验一次 access_token")] = False,
    credentials: CredentialsOpt = None,
    http: Annotated[
        str, typer.Option("--http", help="改成起本地 HTTP 接口，如 127.0.0.1:8765")
    ] = "",
    http_token: Annotated[str, typer.Option("--http-token", help="HTTP 接口的访问令牌")] = "",
    allow_remote: Annotated[
        bool, typer.Option("--allow-remote", help="允许 HTTP 接口绑定非回环地址（危险）")
    ] = False,
    expose_secret: Annotated[
        bool, typer.Option("--expose-secret", help="允许 /qr/wait 返回 AppSecret")
    ] = False,
    exit_after_login: Annotated[
        bool, typer.Option("--exit-after-login", help="HTTP 模式下，绑定成功就退出")
    ] = False,
) -> None:
    """扫码绑定 QQ 机器人：手机 QQ 扫一下，AppID/AppSecret 自动落盘。"""
    settings = _settings(repo, workspace)
    store = _store(credentials)
    protocol = QQBotProtocol(env=env)
    saved: dict[str, Any] = {}
    shutdown = threading.Event()

    def on_connected(result: QrLoginResult) -> None:
        bound = account_from_bind(
            app_id=result.app_id,
            app_secret=result.app_secret,
            user_openid=result.user_openid,
            account=account,
            env=env,
            source="qr",
            markdown_support=markdown,
        )
        saved["path"] = store.save(bound)
        saved["account"] = bound
        if exit_after_login:
            shutdown.set()

    def on_status(text: str) -> None:
        dim_note(text)

    if http:
        _login_over_http(
            protocol=protocol,
            settings=settings,
            account=account,
            source=source,
            http=http,
            http_token=http_token,
            allow_remote=allow_remote,
            expose_secret=expose_secret,
            timeout=timeout,
            poll=poll,
            max_refreshes=max_refreshes,
            on_connected=on_connected,
            shutdown=shutdown,
        )
    else:
        _login_in_terminal(
            protocol=protocol,
            account=account,
            source=source,
            timeout=timeout,
            poll=poll,
            max_refreshes=max_refreshes,
            png=png,
            no_qr=no_qr,
            no_ansi=no_ansi,
            on_connected=on_connected,
            on_status=on_status,
        )

    protocol.close()

    if "account" not in saved:
        console.print("[red]扫码登录没有完成。[/red]")
        raise typer.Exit(1)

    bound = saved["account"]
    console.print(
        Panel(
            f"[green]绑定成功[/green] · AppID {bound.app_id}\n"
            f"凭证已写入 {saved['path']}（权限 600，不入库）",
            title="QQBot",
        )
    )
    if test:
        _verify_token(bound, env)


def _login_in_terminal(
    *,
    protocol: QQBotProtocol,
    account: str,
    source: str,
    timeout: float,
    poll: float,
    max_refreshes: int,
    png: Path | None,
    no_qr: bool,
    no_ansi: bool,
    on_connected: Any,
    on_status: Any,
) -> QrLoginResult | None:
    """在终端里出示二维码并等扫码。返回结果（取消/失败时为 ``None`` 或失败结果）。"""

    def on_qr(url: str, attempt: int, _data_url: str | None) -> None:
        console.print(
            Panel(
                f"第 {attempt} 张二维码（{timeout:.0f} 秒内有效）\n\n{url}",
                title="请用手机 QQ 扫码绑定",
                subtitle="也可以把链接发到手机上打开",
            )
        )
        if not no_qr:
            _print_qr(url, ansi=not no_ansi)
        if png is not None and attempt == 1:
            written = save_png(url, png)
            if written is None:
                console.print("[yellow]没装 qrcode/Pillow，PNG 没写出来。[/yellow]")
            else:
                console.print(f"二维码已存到 [bold]{written}[/bold]")

    flow = QrLoginFlow(
        protocol,
        source=source,
        poll_interval=poll,
        qr_timeout=timeout,
        max_refreshes=max_refreshes,
        on_qr=on_qr,
        on_status=on_status,
    )
    try:
        result = flow.run()
    except KeyboardInterrupt:
        flow.cancel()
        console.print("\n[yellow]已取消扫码登录。[/yellow]")
        return None
    if result.connected:
        on_connected(result)
    else:
        console.print(f"[red]{result.message}[/red]")
    return result


def _login_over_http(
    *,
    protocol: QQBotProtocol,
    settings: Settings,
    account: str,
    source: str,
    http: str,
    http_token: str,
    allow_remote: bool,
    expose_secret: bool,
    timeout: float,
    poll: float,
    max_refreshes: int,
    on_connected: Any,
    shutdown: threading.Event,
) -> None:
    host, port = _parse_host_port(http)
    manager = QrLoginManager(
        protocol,
        source=source,
        with_data_url=True,
        on_connected=on_connected,
        on_event=dim_note,
        flow_kwargs={
            "poll_interval": poll,
            "qr_timeout": timeout,
            "max_refreshes": max_refreshes,
        },
    )
    server = LoginHttpServer(
        manager,
        host=host,
        port=port,
        token=http_token,
        allow_remote=allow_remote,
        expose_secret=expose_secret,
        log=dim_note,
        shutdown_event=shutdown,
    )
    console.print(
        Panel(
            f"扫码登录接口：[bold]{server.url}[/bold]\n"
            "  GET  /            手机浏览器打开就能看到二维码\n"
            "  POST /qr/start    开始一次会话（返回 qrDataUrl）\n"
            "  GET  /qr/wait     等扫码结果\n"
            "  POST /qr/cancel   取消\n"
            "  GET  /qr/status   非阻塞查状态\n"
            "\nCtrl-C 退出",
            title="yqa qq login --http",
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[yellow]已停止扫码登录接口。[/yellow]")
        server.shutdown()


def _verify_token(account: Any, env: str) -> None:
    client = QQBotClient(account, protocol=QQBotProtocol(env=env))
    try:
        client.get_access_token()
        console.print("[green]access_token 获取成功，凭证可用。[/green]")
    except QQBotError as exc:
        console.print(f"[red]access_token 获取失败：{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        client.close()


def _parse_host_port(
    text: str, default_host: str = "127.0.0.1", default_port: int = 8765
) -> tuple[str, int]:
    raw = (text or "").strip()
    if not raw:
        return default_host, default_port
    if raw.isdigit():
        return default_host, int(raw)
    if ":" in raw:
        host, _, port = raw.rpartition(":")
        try:
            return (host or default_host), int(port)
        except ValueError as exc:
            raise typer.BadParameter(f"--http 端口不是数字：{text!r}") from exc
    return raw, default_port


def _print_qr(url: str, *, ansi: bool) -> None:
    art = terminal_qr(url, ansi=ansi)
    if art is None:
        console.print(
            "[yellow]没装 qrcode，画不出终端二维码；把上面的链接复制到手机打开即可"
            "（pip install qrcode）。[/yellow]"
        )
        return
    # 直接写 stdout：ANSI 转义交给终端，别让 rich 重新排版二维码
    sys.stdout.write("\n" + art + "\n\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- status / doctor


@qq_app.command("status")
def qq_status(
    account: AccountOpt = "default",
    workspace: WorkspaceOpt = Path("workspace"),
    repo: RepoOpt = DEFAULT_REPO,
    credentials: CredentialsOpt = None,
    env: EnvOpt = "production",
    check: Annotated[bool, typer.Option("--check", help="联网验一次 access_token")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="输出 JSON")] = False,
) -> None:
    """看绑定状态、配置体检、通知积压。"""
    settings = _settings(repo, workspace)
    payload = qq_status_payload(
        settings, account=account, credentials=credentials, check=check, env=env
    )
    if as_json:
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    table = Table(title="QQBot 状态", show_lines=False)
    table.add_column("项", style="bold")
    table.add_column("结果")
    for key, value in payload["rows"]:
        table.add_row(key, value)
    console.print(table)


def qq_status_payload(
    settings: Settings,
    *,
    account: str = "default",
    credentials: Path | None = None,
    check: bool = False,
    env: str = "production",
) -> dict[str, Any]:
    """``status``/``doctor``/``yqa doctor`` 共用的数据源（默认不联网）。"""
    store = _store(credentials)
    resolved = resolve_account(account=account, store=store)
    config = _config(settings)
    stats = NotifyBridge(notify_dir=settings.notify_dir, sender=NullSender(), config=config).stats()

    rows: list[tuple[str, str]] = []
    if resolved.complete:
        rows.append(("凭证", f"[green]已绑定[/green] · {resolved.describe()}"))
    else:
        rows.append(("凭证", "[yellow]未绑定[/yellow]（跑 yqa qq login 扫码）"))
    rows.append(("凭证文件", f"{store.path}（{'存在' if store.path.exists() else '不存在'}）"))
    rows.append(("配置文件", f"{config.path or default_config_path(settings)}"))
    rows.append(
        (
            "成员映射",
            f"{len(config.members)} 条；兜底 {config.notify_default.to_str() if config.notify_default else '(无)'}",
        )
    )
    rows.append(
        (
            "入站命令",
            f"{'开' if config.inbound_enabled else '关'}；白名单 {len(config.inbound_allow)} 人 / 管理员 {len(config.inbound_admins)} 人",
        )
    )
    rows.append(
        (
            "通知积压",
            f"pending {stats['pending']} · done {stats['done']} · unrouted {stats['unrouted']} · failed {stats['failed']}",
        )
    )
    rows.append(("二维码", support_note()))
    rows.append(("网关 websockets", _websockets_note()))

    token_note = "(未检查；加 --check 联网验证)"
    if check and resolved.complete:
        if env:
            resolved.env = env
        client = QQBotClient(resolved, protocol=QQBotProtocol(env=resolved.env))
        try:
            client.get_access_token()
            token_note = "[green]OK[/green]"
        except QQBotError as exc:
            token_note = f"[red]{exc}[/red]"
        finally:
            client.close()
    rows.append(("access_token", token_note))

    problems = config.problems()
    rows.append(
        (
            "配置体检",
            "[green]没问题[/green]" if not problems else "[yellow]；".join(problems) + "[/yellow]",
        )
    )

    return {
        "account": resolved.account,
        "appId": resolved.app_id,
        "bound": resolved.complete,
        "credentialsPath": str(store.path),
        "configPath": str(config.path) if config.path else "",
        "notify": stats,
        "inbound": {
            "enabled": config.inbound_enabled,
            "allow": list(config.inbound_allow),
            "admins": list(config.inbound_admins),
        },
        "problems": problems,
        "rows": rows,
    }


def _websockets_note() -> str:
    try:
        import websockets  # noqa: F401
    except ImportError:
        return "[yellow]没装（收消息不可用；pip install websockets）[/yellow]"
    return "[green]OK[/green]"


def qq_doctor_rows(settings: Settings) -> list[tuple[str, str]]:
    """给主 ``yqa doctor`` 表加几行（不联网、不抛异常）。"""
    try:
        payload = qq_status_payload(settings)
    except Exception as exc:  # noqa: BLE001 - doctor 自己不能崩
        return [("QQBot", f"[red]{type(exc).__name__}: {exc}[/red]")]
    wanted = {"凭证", "入站命令", "通知积压", "二维码"}
    return [(key, value) for key, value in payload["rows"] if key in wanted]


@qq_app.command("doctor")
def qq_doctor(
    workspace: WorkspaceOpt = Path("workspace"),
    repo: RepoOpt = DEFAULT_REPO,
    account: AccountOpt = "default",
    credentials: CredentialsOpt = None,
    check: Annotated[bool, typer.Option("--check", help="联网验一次 access_token")] = False,
) -> None:
    """自检：依赖、凭证、配置、二维码、网关。"""
    settings = _settings(repo, workspace)
    payload = qq_status_payload(settings, account=account, credentials=credentials, check=check)
    table = Table(title="yuque-agent · QQBot 自检")
    table.add_column("项", style="bold")
    table.add_column("结果")
    for key, value in payload["rows"]:
        table.add_row(key, value)
    console.print(table)
    if not has_qr_support():
        console.print("[yellow]提示：pip install qrcode 之后终端就能直接画出二维码。[/yellow]")


@qq_app.command("logout")
def qq_logout(
    account: AccountOpt = "default",
    credentials: CredentialsOpt = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="不确认直接删")] = False,
) -> None:
    """删掉本地的机器人凭证。"""
    store = _store(credentials)
    resolved = store.get(account)
    if resolved is None:
        console.print(f"[yellow]账户 {account} 没有本地凭证。[/yellow]")
        return
    if not yes:
        typer.confirm(f"确认删除账户 {account}（AppID {resolved.app_id}）的凭证？", abort=True)
    store.delete(account)
    console.print(f"[green]已删除账户 {account} 的凭证。[/green]")


# ---------------------------------------------------------------- send / notify


@qq_app.command("send")
def qq_send(
    to: Annotated[
        str, typer.Option("--to", "-t", help="目标：c2c:<openid> 或 group:<group_openid>")
    ],
    text: Annotated[str, typer.Option("--text", help="消息正文")] = "",
    file: Annotated[Path | None, typer.Option("--file", help="从文件读正文")] = None,
    markdown: Annotated[bool, typer.Option("--markdown", help="按 Markdown 发（需权限）")] = False,
    msg_id: Annotated[str, typer.Option("--msg-id", help="被动回复的目标消息 id")] = "",
    account: AccountOpt = "default",
    credentials: CredentialsOpt = None,
    env: EnvOpt = "production",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只打印不发送")] = False,
) -> None:
    """手动发一条消息（联调用）。"""
    body = text
    if file is not None:
        body = file.read_text(encoding="utf-8")
    if not body.strip():
        console.print("[red]--text 或 --file 至少给一个。[/red]")
        raise typer.Exit(1)
    try:
        target = Target.parse(to, msg_id=msg_id or None)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    sender, protocol = _client(
        account=account, credentials=credentials, env=env, dry_run=dry_run, log=plain_log
    )
    try:
        result = sender.send_text(target, body, markdown=markdown or None)  # type: ignore[attr-defined]
    except QQBotError as exc:
        console.print(f"[red]发送失败：{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        if protocol is not None:
            protocol.close()
    console.print(
        f"[green]已发给 {target.to_str()}[/green] {json.dumps(result, ensure_ascii=False)}"
    )


@qq_app.command("notify")
def qq_notify(
    workspace: WorkspaceOpt = Path("workspace"),
    repo: RepoOpt = DEFAULT_REPO,
    account: AccountOpt = "default",
    credentials: CredentialsOpt = None,
    env: EnvOpt = "production",
    limit: Annotated[int, typer.Option("--limit", "-n", help="本轮最多投几条")] = 20,
    file: Annotated[Path | None, typer.Option("--file", help="只投指定的一个通知文件")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只打印不发送、不移动文件")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="输出 JSON")] = False,
) -> None:
    """把 outbox/notify/pending/ 里的通知投递到 QQ。"""
    settings = _settings(repo, workspace)
    config = _config(settings)
    sender, protocol = _client(
        account=account, credentials=credentials, env=env, dry_run=dry_run, log=plain_log
    )
    bridge = NotifyBridge(
        notify_dir=settings.notify_dir,
        sender=sender,
        config=config,
        dry_run=dry_run,
        log=None if as_json else plain_log,
    )
    try:
        if file is not None:
            results = [bridge.deliver(file)]
        else:
            results = bridge.drain(limit=limit)
    finally:
        if protocol is not None:
            protocol.close()

    if as_json:
        console.print_json(json.dumps([item.to_dict() for item in results], ensure_ascii=False))
        return
    if not results:
        console.print("[dim]pending 是空的，没有要投的通知。[/dim]")
        return
    for item in results:
        style = {"delivered": "green", "dry_run": "cyan", "unrouted": "yellow"}.get(
            item.status, "red"
        )
        console.print(f"[{style}]{item.describe()}[/{style}]")
    stat = bridge.stats()
    console.print(
        f"[dim]pending {stat['pending']} · done {stat['done']} · "
        f"unrouted {stat['unrouted']} · failed {stat['failed']}[/dim]"
    )


# ---------------------------------------------------------------- config


@qq_app.command("config")
def qq_config(
    workspace: WorkspaceOpt = Path("workspace"),
    repo: RepoOpt = DEFAULT_REPO,
    init: Annotated[bool, typer.Option("--init", help="写一份空模板（已存在就不动）")] = False,
    show: Annotated[bool, typer.Option("--show", help="打印当前配置")] = False,
) -> None:
    """看 / 初始化 ``qqbot.json``（成员映射 + 入站白名单）。"""
    settings = _settings(repo, workspace)
    path = default_config_path(settings)
    if init:
        written = init_config(settings)
        console.print(f"[green]配置在 {written}[/green]（已存在则不改动）")
    config = _config(settings)
    if show or not init:
        console.print_json(json.dumps(config.to_dict(), ensure_ascii=False))
    problems = config.problems()
    if problems:
        console.print("[yellow]体检：[/yellow]")
        for item in problems:
            console.print(f"  · {item}")
    console.print(f"[dim]路径：{path}[/dim]")
    console.print(f"[dim]{HELP_TEXT.splitlines()[0]}[/dim]")


# ---------------------------------------------------------------- serve


@qq_app.command("serve")
def qq_serve(
    workspace: WorkspaceOpt = Path("workspace"),
    repo: RepoOpt = DEFAULT_REPO,
    account: AccountOpt = "default",
    credentials: CredentialsOpt = None,
    env: EnvOpt = "production",
    interval: Annotated[int, typer.Option("--interval", "-i", help="轮询间隔（秒）")] = 60,
    quiet_seconds: Annotated[
        int | None, typer.Option("--quiet-seconds", help="静默期（秒）；0=关闭")
    ] = None,
    notify_interval: Annotated[
        float, typer.Option("--notify-interval", help="通知泵间隔（秒）")
    ] = 5.0,
    no_watch: Annotated[bool, typer.Option("--no-watch", help="不轮询，只投通知 + 收命令")] = False,
    no_inbound: Annotated[bool, typer.Option("--no-inbound", help="不连网关收消息")] = False,
    no_login: Annotated[
        bool,
        typer.Option(
            "--no-login", help="没有缓存凭证时不要自动扫码登录，直接报错（给 systemd 用）"
        ),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="写操作只记录不执行")] = False,
    journal: Annotated[bool, typer.Option("--journal", help="把 session 写回工作日志")] = False,
    model: Annotated[str, typer.Option("--model")] = DEFAULT_MODEL,
) -> None:
    """常驻：轮询语雀 + 投递通知 + 接 QQ 命令（``/status`` ``/run`` …）。

    **第一次直接跑就行**：没有 ``~/.yuque/qqbot.json`` 时会在终端里出示二维码，
    手机 QQ 扫一下，凭证自动落盘，然后服务继续启动。加 ``--no-login`` 可关掉这个行为。
    """
    settings = _settings(
        repo, workspace, dry_run=dry_run, journal=journal, model=model, interval=interval
    )
    if quiet_seconds is not None:
        settings.quiet_seconds = quiet_seconds
    config = _config(settings)

    if not settings.token and not dry_run:
        # 先确认语雀这边是通的：否则扫完码才发现缺 token，白扫一次
        console.print(
            "[red]缺少语雀 token（设 YQA_TOKEN / YUQUE_TOKEN）——先配好语雀再启动服务。[/red]"
        )
        raise typer.Exit(1)

    sender, protocol = _client(
        account=account,
        credentials=credentials,
        env=env,
        dry_run=dry_run,
        log=plain_log,
        login_if_needed=not no_login,
    )
    client = sender if isinstance(sender, QQBotClient) else None

    yuque = YuqueClient(
        host=settings.host, token=settings.token, repo=settings.repo, dry_run=settings.dry_run
    )
    llm = LLMClient(base_url=settings.api_base, api_key=settings.api_key, model=settings.model)
    runner = Runner(settings=settings, client=yuque, llm=llm)
    watcher = Watcher(runner=runner, settings=settings, log=plain_log)
    bridge = NotifyBridge(
        notify_dir=settings.notify_dir,
        sender=sender,
        config=config,
        dry_run=dry_run,
        log=plain_log,
    )
    service = QQBotService(
        settings=settings,
        runner=runner,
        watcher=watcher,
        bridge=bridge,
        config=config,
        qq_client=client,
        log=plain_log,
        notify_interval=notify_interval,
    )
    console.print(
        Panel(
            f"知识库 {settings.repo}\n"
            f"轮询 {'开' if not no_watch else '关'}（每 {interval}s，静默期 {settings.quiet_seconds}s）\n"
            f"通知泵 每 {notify_interval}s 扫一次 outbox/notify/pending/\n"
            f"QQ 入站 {'关（--no-inbound）' if no_inbound else ('开' if client else '关（未绑定）')}"
            f" · 白名单 {len(config.inbound_allow)} 人 / 管理员 {len(config.inbound_admins)} 人",
            title="yqa qq serve",
        )
    )
    try:
        service.serve(watch=not no_watch, inbound=not no_inbound)
    except KeyboardInterrupt:
        console.print("\n[dim]已停止。[/dim]")
    finally:
        service.stop()
        yuque.close()
        llm.close()
        if protocol is not None:
            protocol.close()


def main() -> None:  # pragma: no cover - 供 `python -m yuque_agent.qqbot.cli` 调试
    qq_app()


__all__ = ["auto_login", "make_sender", "qq_app", "qq_doctor_rows", "qq_status_payload"]
