"""命令行入口。

常用：

```bash
yqa doctor                      # 自检：token / 权限 / 知识库 / 模型 / 提示词
yqa once                        # 跑一轮轮询（没有变化就什么都不做）
yqa once --force                # 无视 diff，强制唤醒一次
yqa archive                     # 手动跑一次归档会话
yqa run                         # 常驻轮询 + 每周六自动归档
yqa sessions                    # 看本地留了哪些 run
yqa render <run_id>             # 把某次 run 的 session 渲染成人话
```
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from . import journal as journal_mod
from .config import DEFAULT_API_BASE, DEFAULT_HOST, DEFAULT_MODEL, DEFAULT_REPO, Settings
from .llm import LLMClient, LLMError
from .outputs import build_plan_json
from .prompts import PromptLoader
from .qqbot.cli import qq_app, qq_doctor_rows
from .runner import Runner, new_run_id
from .watcher import Watcher
from .yuque import YuqueClient, YuqueError


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，打中文/符号会 UnicodeEncodeError；尽力切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


_force_utf8()

app = typer.Typer(
    add_completion=False,
    rich_markup_mode="markdown",
    help="让 LLM 全权接管一个语雀知识库：程序只做感知与留痕，判断权归 LLM。",
)
console = Console()

app.add_typer(qq_app, name="qq")


def _plain_log(text: str) -> None:
    """原样打日志：``[watch]`` / ``[qqbot:notify]`` 这类前缀不能被 rich 当 markup 吃掉。"""
    console.print(text, markup=False, highlight=False)


def _settings(
    repo: str,
    workspace: Path,
    dry_run: bool,
    journal: bool,
    model: str,
    interval: int,
    verbose: bool,
) -> Settings:
    settings = Settings.from_env(
        repo=repo,
        workspace=workspace,
        model=model,
        interval=interval,
        dry_run=dry_run,
        journal=journal,
        verbose=verbose,
    )
    return settings


RepoOpt = Annotated[str, typer.Option("--repo", "-r", help="知识库 namespace")]


@app.command()
def version() -> None:
    """打印版本。"""
    console.print(f"yuque-agent {__version__}")


@app.command()
def doctor(
    repo: RepoOpt = DEFAULT_REPO,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    model: Annotated[str, typer.Option("--model")] = DEFAULT_MODEL,
) -> None:
    """自检：凭证、权限、知识库连通、提示词、工作区。"""
    settings = _settings(repo, workspace, False, False, model, 60, False)
    table = Table(title="yuque-agent 自检", show_lines=False)
    table.add_column("项", style="bold")
    table.add_column("结果")

    ok = "[green]OK[/green]"
    table.add_row("python", sys.version.split()[0])
    table.add_row("repo", settings.repo)
    table.add_row("workspace", str(settings.root.resolve()))
    table.add_row("语雀 token", ok if settings.token else "[red]未找到（设 YQA_TOKEN）[/red]")
    table.add_row("LLM key", ok if settings.api_key else "[red]未找到（设 DEEPSEEK_API_KEY）[/red]")
    table.add_row("LLM 端点", f"{settings.api_base} · {settings.model}")

    for key, value in qq_doctor_rows(settings):
        table.add_row(key, value)

    for kind in ("polling", "archive"):
        try:
            PromptLoader().load(kind)
            table.add_row(f"提示词 {kind}", ok)
        except Exception as exc:  # noqa: BLE001
            table.add_row(f"提示词 {kind}", f"[red]{exc}[/red]")

    if settings.token:
        try:
            with YuqueClient(
                host=settings.host, token=settings.token, repo=settings.repo
            ) as client:
                info = client.repo_info()
                table.add_row("知识库", f"{ok} {info.get('name')}（{info.get('items_count')} 篇）")
                table.add_row("scope", client.scopes or "(未返回)")
                toc = client.toc()
                docs = client.docs()
                table.add_row("目录节点 / 文档", f"{len(toc)} / {len(docs)}")
                scopes = {s.strip() for s in (client.scopes or "").split(",")}
                can_write = bool(scopes & {"repo", "doc", "group"})
                table.add_row("写权限", ok if can_write else "[yellow]无（归档会失败）[/yellow]")
        except YuqueError as exc:
            table.add_row("知识库", f"[red]{exc}[/red]")

    console.print(table)


def _clients(settings: Settings) -> tuple[YuqueClient, LLMClient]:
    return (
        YuqueClient(
            host=settings.host, token=settings.token, repo=settings.repo, dry_run=settings.dry_run
        ),
        LLMClient(base_url=settings.api_base, api_key=settings.api_key, model=settings.model),
    )


def _poll_skip_message(runner: Runner, settings: Settings) -> str:
    """本轮没唤醒 LLM 时，该对用户说什么。

    ``poll_once`` 返回 ``None`` 有**两种**原因（没变化 / 还在静默期），
    一律说「没有变化」就会在静默期里说假话。
    """
    if runner.last_skip == "quiet_period":
        return f"i 知识库刚变过，还在 {settings.quiet_seconds}s 静默期内，本轮未唤醒 LLM。"
    return "i 知识库没有变化，未唤醒 LLM。"


@app.command()
def once(
    repo: RepoOpt = DEFAULT_REPO,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="无视 diff，即使没有变化也唤醒 LLM（给存量文档做体检时用）",
        ),
    ] = False,
    rescan: Annotated[
        bool,
        typer.Option("--rescan", help="无视快照，把现有全部文档重新评估一遍（会重发通知）"),
    ] = False,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="所有写操作只记录不执行")] = False,
    journal: Annotated[
        bool, typer.Option("--journal", help="把 session 写回语雀《工作日志》")
    ] = False,
    model: Annotated[str, typer.Option("--model")] = DEFAULT_MODEL,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="没变化时不打印")] = False,
) -> None:
    """跑一轮轮询。没有变化 → 什么都不做（0 token）。

    这是**人工命令**，所以会关掉静默期合并（``debounce=False``）：你刚写完文档
    敲下 ``yqa once``，就该立刻看到结果，而不是被告知「没有变化」然后白等 45 秒。
    静默期只对常驻轮询（``yqa run``）有意义——那里的变更来自语雀的分步投稿，
    合并能把一篇文档从 13 次唤醒压到 1 次。

    ``--force`` 和静默期的边界（容易搞混，所以写在这里）：

    * **在命令行这一层**：``once`` 已经关掉了静默期，所以 ``--force`` 不 ``--force``
      都会真跑，只要你敲了命令。
    * **在 ``Runner.poll_once`` 这一层**：``force=True`` **不**绕过静默期
      —— ``--force`` 的含义是「无视 diff」，不是「无视静默期」。这是有意设计，
      由 ``tests/test_debounce.py::test_force_bypasses_nothing_but_still_needs_quiet`` 锁住。
      如果你绕开 CLI 直接调 ``poll_once(force=True)``，静默期内仍然不会跑。
    """
    settings = _settings(repo, workspace, dry_run, journal, model, 60, False)
    client, llm = _clients(settings)
    try:
        runner = Runner(settings=settings, client=client, llm=llm)
        result = runner.poll_once(force=force, rescan=rescan, debounce=False)
    finally:
        client.close()
        llm.close()

    if result is None:
        if not quiet:
            console.print(f"[dim]{_poll_skip_message(runner, settings)}[/dim]")
        return
    _print_result(result)


@app.command()
def archive(
    repo: RepoOpt = DEFAULT_REPO,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="所有写操作只记录不执行")] = False,
    journal: Annotated[
        bool, typer.Option("--journal", help="把 session 写回语雀《工作日志》")
    ] = False,
    model: Annotated[str, typer.Option("--model")] = DEFAULT_MODEL,
) -> None:
    """手动跑一次归档会话（带结构写工具的那个）。"""
    settings = _settings(repo, workspace, dry_run, journal, model, 60, False)
    client, llm = _clients(settings)
    try:
        runner = Runner(settings=settings, client=client, llm=llm)
        result = runner.archive_once()
    finally:
        client.close()
        llm.close()
    _print_result(result)


@app.command()
def run(
    repo: RepoOpt = DEFAULT_REPO,
    interval: Annotated[int, typer.Option("--interval", "-i", help="轮询间隔（秒）")] = 60,
    quiet_seconds: Annotated[
        int | None,
        typer.Option(
            "--quiet-seconds",
            help="静默期（秒）：发现变化后先不叫 LLM，等知识库安静这么久再一次性处理；0=关闭",
        ),
    ] = None,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    journal: Annotated[bool, typer.Option("--journal")] = False,
    model: Annotated[str, typer.Option("--model")] = DEFAULT_MODEL,
    max_ticks: Annotated[
        int | None, typer.Option("--max-ticks", help="跑几轮就退出（自测用）")
    ] = None,
    qq: Annotated[
        bool, typer.Option("--qq", help="每轮结束后把 outbox/notify 的通知投递到 QQ")
    ] = False,
    qq_account: Annotated[
        str, typer.Option("--qq-account", help="用哪个 QQBot 账户投递")
    ] = "default",
    qq_no_login: Annotated[
        bool,
        typer.Option("--qq-no-login", help="没有缓存凭证时不要自动扫码登录，直接报错"),
    ] = False,
) -> None:
    """常驻：轮询 + 每周六 00:00 自动归档。

    默认开启**静默期合并**：语雀手工建一篇文档会分几步产生变更
    （无标题空文档 → 改标题 → 写正文保存），不合并的话一篇文档就要唤醒好几次 LLM。

    加 ``--qq`` 就顺便当投递方：每轮结束后把 ``outbox/notify/pending/`` 里的通知发到 QQ；
    **没有缓存凭证时会先在终端里出示二维码**（手机 QQ 扫一下，凭证落盘后继续启动），
    不想这样用 ``--qq-no-login``。
    要收 QQ 命令（``/status`` ``/run``）用 ``yqa qq serve``。
    """
    settings = _settings(repo, workspace, dry_run, journal, model, interval, False)
    if quiet_seconds is not None:
        settings.quiet_seconds = quiet_seconds
    client, llm = _clients(settings)
    bridge = None
    qq_protocol = None
    try:
        runner = Runner(settings=settings, client=client, llm=llm)
        after_tick = None
        if qq:
            from .qqbot.bridge import NotifyBridge
            from .qqbot.cli import make_sender
            from .qqbot.config import QQBotConfig, default_config_path

            sender, qq_protocol = make_sender(
                account=qq_account,
                dry_run=dry_run,
                log=_plain_log,
                login_if_needed=not qq_no_login,
            )
            qq_config = QQBotConfig.load(default_config_path(settings))
            bridge = NotifyBridge(
                notify_dir=settings.notify_dir,
                sender=sender,
                config=qq_config,
                dry_run=dry_run,
                log=_plain_log,
            )
            after_tick = bridge.drain
            console.print(
                f"[dim]QQ 投递已开启：每轮结束后扫 {settings.notify_dir / 'pending'}[/dim]"
            )
        Watcher(runner=runner, settings=settings, log=_plain_log).run_forever(
            max_ticks=max_ticks, after_tick=after_tick
        )
    except KeyboardInterrupt:
        console.print("\n[dim]已停止。[/dim]")
    finally:
        client.close()
        llm.close()
        if qq_protocol is not None:
            qq_protocol.close()


@app.command()
def sessions(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    repo: RepoOpt = DEFAULT_REPO,
) -> None:
    """列出本地留档的所有 run。"""
    settings = _settings(repo, workspace, False, False, DEFAULT_MODEL, 60, False)
    runs = sorted(settings.runs_dir.glob("*/session.jsonl")) if settings.runs_dir.exists() else []
    if not runs:
        console.print("[dim]还没有任何 run。[/dim]")
        return
    table = Table(title=f"{len(runs)} 个 run")
    table.add_column("run_id")
    table.add_column("大小")
    table.add_column("路径")
    for path in runs:
        table.add_row(path.parent.name, f"{path.stat().st_size} B", str(path.parent))
    console.print(table)


@app.command()
def render(
    run_id: Annotated[str, typer.Argument(help="run_id 或 session.jsonl 的路径")],
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    repo: RepoOpt = DEFAULT_REPO,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="写入文件而不是打印")] = None,
) -> None:
    """把某次 run 的 session 渲染成人话。"""
    settings = _settings(repo, workspace, False, False, DEFAULT_MODEL, 60, False)
    path = Path(run_id)
    if not path.is_file():
        path = settings.runs_dir / run_id / "session.jsonl"
    if not path.is_file():
        console.print(f"[red]找不到 session：{run_id}[/red]")
        raise typer.Exit(1)
    text = journal_mod.render_session(path)
    if out:
        out.write_text(text, encoding="utf-8")
        console.print(f"已写入 {out}")
    else:
        console.print(text)


@app.command()
def journal(
    run_id: Annotated[str, typer.Argument(help="run_id")],
    repo: RepoOpt = DEFAULT_REPO,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """把某次 run 的 session 写回语雀《工作日志》。"""
    settings = _settings(repo, workspace, dry_run, True, DEFAULT_MODEL, 60, False)
    path = settings.runs_dir / run_id / "session.jsonl"
    if not path.is_file():
        console.print(f"[red]找不到 session：{path}[/red]")
        raise typer.Exit(1)
    with YuqueClient(
        host=settings.host, token=settings.token, repo=settings.repo, dry_run=settings.dry_run
    ) as client:
        outcome = journal_mod.journal_or_warn(client, settings, path)
    if outcome.get("ok"):
        console.print(f"[green][OK] 已写入《{settings.journal_title}》[/green] {outcome}")
    else:
        console.print(f"[red][FAIL] {outcome.get('error')}[/red]")
        raise typer.Exit(1)


@app.command("export-plan")
def export_plan(
    repo: RepoOpt = DEFAULT_REPO,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    out: Annotated[Path | None, typer.Option("--out", "-o", help="写入文件；默认打印")] = None,
    defaults: Annotated[str, typer.Option("--defaults", help="defaults 对象的 JSON 字符串")] = "",
) -> None:
    """把 `outbox/applications/` 汇总成下游可直接吃的 `plan.json`。

    下游用法（以 crb 为例）：

        yqa export-plan -o plan.json --defaults '{"JYDWDM":"400760","JSJYLXDM":"02"}'
        crb plan --file plan.json          # 先看方案
        crb plan --file plan.json --save   # 再存草稿
    """
    settings = _settings(repo, workspace, False, False, DEFAULT_MODEL, 60, False)
    parsed: dict = {}
    if defaults:
        try:
            parsed = json.loads(defaults)
        except ValueError as exc:
            console.print(f"[red]--defaults 不是合法 JSON：{exc}[/red]")
            raise typer.Exit(1) from exc
    plan = build_plan_json(settings, defaults=parsed)
    text = json.dumps(plan, ensure_ascii=False, indent=2)
    if out:
        out.write_text(text, encoding="utf-8")
        console.print(f"[green]已写入 {out}[/green]（{len(plan['activities'])} 条活动）")
    else:
        console.print_json(text)


@app.command("sync-guide")
def sync_guide(
    repo: RepoOpt = DEFAULT_REPO,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """把 `kb/guide.md` 上传/更新为知识库里的《指导文档（必读）》。"""
    settings = _settings(repo, Path("workspace"), dry_run, False, DEFAULT_MODEL, 60, False)
    body = (Path(__file__).parent / "kb" / "guide.md").read_text(encoding="utf-8")
    title = "指导文档（必读）"
    with YuqueClient(
        host=settings.host, token=settings.token, repo=settings.repo, dry_run=settings.dry_run
    ) as client:
        existing = next((m for m in client.docs() if m.title == title), None)
        if existing is None:
            if settings.dry_run:
                console.print("[yellow]dry-run：将新建《指导文档（必读）》[/yellow]")
                return
            created = client.create_doc(title=title, body=body)
            doc_id = int((created or {}).get("id") or 0)
            if doc_id:
                client.toc_add(doc_ids=[doc_id])
            console.print(f"[green][OK] 已新建《{title}》（doc_id={doc_id}）[/green]")
        else:
            if settings.dry_run:
                console.print(
                    f"[yellow]dry-run：将更新《{title}》（doc_id={existing.doc_id}）[/yellow]"
                )
                return
            client.update_doc(existing.doc_id, body=body)
            console.print(f"[green][OK] 已更新《{title}》（doc_id={existing.doc_id}）[/green]")


def _print_result(result) -> None:
    payload = result.to_dict()
    usage = payload["usage"]
    head = f"{result.verdict or '—'} · {result.summary or '(无摘要)'}"
    console.print(Panel(head, title=f"{result.kind} · {result.run_id}", style="green"))
    console.print(
        f"steps={payload['steps']} · tools={payload['tool_calls']} · "
        f"tokens(in/out)={usage['in']}/{usage['out']} · stop={payload['stop_reason']}"
    )
    if payload["emitted"]:
        console.print("产出：")
        for item in payload["emitted"]:
            console.print(f"  · {item.get('type')} → {item.get('path') or item.get('notice_id')}")
    if payload["kb_writes"]:
        console.print("语雀写操作：")
        for item in payload["kb_writes"]:
            console.print(f"  · {item}")
    if payload.get("journal"):
        console.print(f"工作日志：{payload['journal']}")
    if payload["error"]:
        console.print(f"[red][!] {payload['error']}[/red]")
    console.print(f"[dim]session: {payload['session_path']}[/dim]")


def main() -> None:
    try:
        app()
    except (YuqueError, LLMError) as exc:
        console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()


__all__ = ["app", "main", "new_run_id", "DEFAULT_HOST", "DEFAULT_API_BASE"]
