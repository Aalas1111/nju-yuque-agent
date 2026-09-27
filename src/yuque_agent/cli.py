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
yqa refresh-notice              # 重建语雀《Agent 通知》（程序维护，幂等）
```
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__, clock, noticedoc, outputs
from . import render as render_mod
from .config import (
    ARCHIVE_ZONE_TITLE,
    DEFAULT_MODEL,
    GUIDE_TITLE,
    NOTICE_TITLE,
    ConfigError,
    Settings,
)
from .llm import LLMClient, LLMError, describe_llm_error
from .outputs import publish_plan, write_plan_defaults
from .planserve import pii_warning
from .planserve import read_plan as read_plan_file
from .planserve import serve as serve_plan_http
from .prompts import PromptLoader
from .runner import Runner, load_state, save_state
from .watcher import Watcher
from .week import cycle_targets
from .yuque import YuqueClient, YuqueError, doc_dir_map


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


def _plain_log(text: str) -> None:
    """原样打日志：``[watch]`` 这类前缀不能被 rich 当 markup 吃掉。"""
    console.print(text, markup=False, highlight=False)


def _settings(
    repo: str,
    workspace: Path,
    dry_run: bool,
    model: str,
    interval: int,
) -> Settings:
    """CLI 参数 > 环境变量 > 凭证文件（见 `config.Settings.from_env`）。

    ``repo`` 空串 = 命令行没给 —— 回退到 ``YQA_REPO``；两边都没有就直接退出
    （**没有默认知识库**：写死一个 namespace 意味着「忘配的人会连到别人的知识库」，
    那比当场停下危险得多）。
    """
    try:
        return Settings.from_env(
            repo=repo or None,
            workspace=workspace,
            model=model,
            interval=interval,
            dry_run=dry_run,
        )
    except ConfigError as exc:
        console.print(str(exc), markup=False)
        raise typer.Exit(2) from exc


RepoOpt = Annotated[
    str,
    typer.Option(
        "--repo",
        "-r",
        help="知识库 namespace（如 group/repo）。不写就看 YQA_REPO；**没有默认知识库**",
    ),
]


@app.command()
def version() -> None:
    """打印版本。"""
    console.print(f"yuque-agent {__version__}")


@app.command()
def doctor(
    repo: RepoOpt = "",
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    model: Annotated[str, typer.Option("--model")] = DEFAULT_MODEL,
) -> None:
    """自检：凭证、权限、知识库连通、提示词、工作区。"""
    settings = _settings(repo, workspace, False, model, 20)
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
    # 时区是**钉死**的：这个项目所有「今天 / 周六 00:00」都按上海算，
    # 不看服务器设置。所以这里把两件事都显示出来，方便一眼确认。
    server_tz = clock.server_tz_name()
    table.add_row(
        "时区",
        f"[green]{clock.TZ_NAME} +08:00[/green]（写死，不受服务器设置影响；"
        f"服务器当前 = {server_tz}）",
    )

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

    # 放最后：前面那些要么读本地、要么读语雀，失败得都快；只有这一项要
    # **真花 token**，所以不挡在前面。它补的是上面「LLM key」那行的盲区——
    # 那行只检查变量在不在，key 过期 / 打错 / 余额耗尽它都显示 OK。
    _probe_llm_row(table, settings)

    # cac 靠下载口取件，所以「现在能下到什么」值得一眼看到。
    _plan_rows(table, settings)

    console.print(table)


def _plan_rows(table: Table, settings: Settings) -> None:
    """交付件与下载口的现状。"""
    plan = read_plan_file(settings)
    if plan is None:
        table.add_row("申请清单", "[yellow]还没有 outbox/plan.json[/yellow]")
    else:
        count = len(plan.get("activities") or [])
        table.add_row("申请清单", f"{plan.get('cycle') or '?'} · {count} 条")
    warning = pii_warning(settings)
    if warning:
        table.add_row("⚠ 清单隐私", f"[red]{warning.splitlines()[0].strip()}[/red]")


def _probe_llm_row(table: Table, settings: Settings) -> None:
    """真打一次 API，确认 key 不只是「存在」而是「能用」。"""
    if not settings.api_key:
        table.add_row("LLM 可用性", "[yellow]跳过（没找到 key）[/yellow]")
        return
    try:
        with LLMClient(
            base_url=settings.api_base,
            api_key=settings.api_key,
            model=settings.model,
            max_retries=1,
            timeout=30.0,
        ) as client:
            usage = client.ping()
    except LLMError as exc:  # noqa: BLE001 - 探测就是为了把失败原因显出来
        table.add_row("LLM 可用性", f"[red]{describe_llm_error(exc)}[/red]")
    else:
        table.add_row(
            "LLM 可用性", f"[green]OK[/green] 真调一次成功（{usage.total_tokens} tokens）"
        )


def _clients(settings: Settings) -> tuple[YuqueClient, LLMClient]:
    return (
        YuqueClient(
            host=settings.host, token=settings.token, repo=settings.repo, dry_run=settings.dry_run
        ),
        LLMClient(base_url=settings.api_base, api_key=settings.api_key, model=settings.model),
    )


def _poll_skip_message(runner: Runner, settings: Settings) -> str:
    """本轮没唤醒 LLM 时，该对用户说什么。

    ``poll_once`` 返回 ``None`` 有**三种**原因（没变化 / 还在静默期 /
    只有归档区在动），一律说「没有变化」就会在静默期与归档区那两种情形说假话。
    """
    if runner.last_skip == "quiet_period":
        return f"i 知识库刚变过，还在 {settings.quiet_seconds}s 静默期内，本轮未唤醒 LLM。"
    if runner.last_skip == "archived_only":
        return "i 变的只有「归档区」里的文档（终点站，程序已忽略），未唤醒 LLM。"
    return "i 知识库没有变化，未唤醒 LLM。"


@app.command()
def once(
    repo: RepoOpt = "",
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
    settings = _settings(repo, workspace, dry_run, model, 20)
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
    repo: RepoOpt = "",
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="所有写操作只记录不执行")] = False,
    model: Annotated[str, typer.Option("--model")] = DEFAULT_MODEL,
) -> None:
    """手动跑一次归档会话（带结构写工具的那个）。"""
    settings = _settings(repo, workspace, dry_run, model, 20)
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
    repo: RepoOpt = "",
    interval: Annotated[int, typer.Option("--interval", "-i", help="轮询间隔（秒）")] = 20,
    quiet_seconds: Annotated[
        int | None,
        typer.Option(
            "--quiet-seconds",
            help="静默期（秒）：发现变化后先不叫 LLM，等知识库安静这么久再一次性处理；0=关闭",
        ),
    ] = None,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    model: Annotated[str, typer.Option("--model")] = DEFAULT_MODEL,
    max_ticks: Annotated[
        int | None, typer.Option("--max-ticks", help="跑几轮就退出（自测用）")
    ] = None,
) -> None:
    """常驻：轮询 + 归档 + 消费 `control/requests/`（**唯一写 state.json 的进程**）。

    默认开启**静默期合并**：语雀手工建一篇文档会分几步产生变更
    （无标题空文档 → 改标题 → 写正文保存），不合并的话一篇文档就要唤醒好几次 LLM。

    外部（QQ 桥等）要触发一轮/归档/申请，写 `control/requests/`，不要自己起轮询——
    见 `docs/interface.md` §1.2（两个写者会互相覆盖快照，那是记过事故的）。
    """
    settings = _settings(repo, workspace, dry_run, model, interval)
    if quiet_seconds is not None:
        settings.quiet_seconds = quiet_seconds
    client, llm = _clients(settings)
    try:
        runner = Runner(settings=settings, client=client, llm=llm)
        Watcher(runner=runner, settings=settings, log=_plain_log).run_forever(max_ticks=max_ticks)
    except KeyboardInterrupt:
        console.print("\n[dim]已停止。[/dim]")
    finally:
        client.close()
        llm.close()


@app.command()
def sessions(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    repo: RepoOpt = "",
) -> None:
    """列出本地留档的所有 run。"""
    settings = _settings(repo, workspace, False, DEFAULT_MODEL, 20)
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
    repo: RepoOpt = "",
    out: Annotated[Path | None, typer.Option("--out", "-o", help="写入文件而不是打印")] = None,
) -> None:
    """把某次 run 的 session 渲染成人话。"""
    settings = _settings(repo, workspace, False, DEFAULT_MODEL, 20)
    path = Path(run_id)
    if not path.is_file():
        path = settings.runs_dir / run_id / "session.jsonl"
    if not path.is_file():
        console.print(f"[red]找不到 session：{run_id}[/red]")
        raise typer.Exit(1)
    text = render_mod.render_session(path)
    if out:
        out.write_text(text, encoding="utf-8")
        console.print(f"已写入 {out}")
    else:
        console.print(text)


@app.command("refresh-notice")
def refresh_notice(
    repo: RepoOpt = "",
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
) -> None:
    """按当前周期的通知重建语雀《Agent 通知》文档（程序维护，幂等）。"""
    settings = _settings(repo, workspace, False, DEFAULT_MODEL, 20)
    with YuqueClient(
        host=settings.host, token=settings.token, repo=settings.repo, dry_run=settings.dry_run
    ) as client:
        outcome = noticedoc.refresh(settings, client)
    if outcome.get("ok"):
        console.print(f"[green][OK]《{settings.notice_title}》已更新[/green] {outcome}")
    else:
        console.print(f"[red][FAIL] {outcome.get('error')}[/red]")
        raise typer.Exit(1)


@app.command("export-plan")
def export_plan(
    repo: RepoOpt = "",
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    out: Annotated[Path | None, typer.Option("--out", "-o", help="写入文件；默认打印")] = None,
    defaults: Annotated[str, typer.Option("--defaults", help="defaults 对象的 JSON 字符串")] = "",
) -> None:
    """把当前周期的申请汇总成下游可直接吃的 `plan.json`。

    它**同时刷新**工作区里的 `outbox/plan.json`——那才是下游（cac）取件的地方。

    下游用法（以 crb 为例）：

        yqa export-plan --defaults '{"JYDWDM":"400760","JSJYLXDM":"02"}'
        crb plan --file plan.json          # 先看方案
        crb plan --file plan.json --save   # 再存草稿

    `--defaults` 会被**落盘保存**（`outbox/plan.defaults.json`）：因为 agent 每次
    写申请都会自动重发 plan.json，不存下来那次重发就把借用人信息丢了。
    """
    settings = _settings(repo, workspace, False, DEFAULT_MODEL, 20)
    parsed: dict = {}
    if defaults:
        try:
            parsed = json.loads(defaults)
        except ValueError as exc:
            console.print(f"[red]--defaults 不是合法 JSON：{exc}[/red]")
            raise typer.Exit(1) from exc
    # 落盘保存：agent 每次写申请都会自动重发 plan.json，
    # 不存下来的话那次重发就把借用人信息丢了。
    if parsed:
        write_plan_defaults(settings, parsed)
    plan = publish_plan(settings, defaults=parsed or None)
    text = json.dumps(plan, ensure_ascii=False, indent=2)
    console.print(
        f"[green]已刷新 {settings.plan_file}[/green]"
        f"（周期 {plan.get('cycle') or '?'}，{len(plan['activities'])} 条活动）",
        markup=True,
    )
    if out:
        out.write_text(text, encoding="utf-8")
        console.print(f"[green]另写入 {out}[/green]")
    else:
        console.print_json(text)


@app.command("serve-plan")
def serve_plan(
    repo: RepoOpt = "",
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    host: Annotated[str, typer.Option("--host", help="监听地址；默认所有网卡")] = "0.0.0.0",
    port: Annotated[int | None, typer.Option("--port", help="默认 YQA_PLAN_PORT 或 8787")] = None,
) -> None:
    """开一个 plan.json 下载口（给 cac 手动取件用）。**没有密钥，打开即下载。**

    为什么不需要它：下游是浏览器里的油猴脚本，**没法直连服务器**，所以取件只能
    靠人工。原来设计了密钥，但——服务器没有域名、只有明文 HTTP，密钥在 URL 里、
    在浏览器历史里、在截图里都会漏。与其维持一个「看着有防护、实际拦不住人」的
    假象，不如干脆不做防护，把「访问即下载」做成一个清楚的事实。

    能做到的（也有测试）：只放行由程序自己算出的那两类文件（路径不可越狱），
    特别是 ``plan.defaults.json``（**借用人姓名与手机号**）永远取不到。
    但它仍是**公开**端点 —— 见 `docs/deploy.md` §10。
    """
    settings = _settings(repo, workspace, False, DEFAULT_MODEL, 20)
    if port is not None:
        settings.plan_port = port
    try:
        serve_plan_http(settings, host=host)
    except OSError as exc:  # 端口被占
        console.print(f"[red]开不了下载口：{exc}[/red]")
        raise typer.Exit(1) from exc


def _fill_notice_link(client: YuqueClient, settings: Settings, body: str) -> str:
    """把 ``guide.md`` 里的 ``{{notice_url}}`` 换成《Agent 通知》**当前**的地址。

    为什么要这一步：那篇文档的 URL 带着语雀生成的 slug——文档一旦被删掉重建，slug 就变了，
    写死的链接会**静默失效**。而它的地址只有程序知道（那篇文档就是程序建的），
    所以在上传前填。找不到那篇文档时整条链接降级成纯文本（社员照样知道去看哪儿）。
    """
    meta = next((m for m in client.docs() if m.title == settings.notice_title), None)
    if meta is None:
        return re.sub(r"\[([^\]]+)\]\(\{\{notice_url\}\}\)", r"\1", body)
    return body.replace(
        "{{notice_url}}", f"{settings.host.rstrip('/')}/{settings.repo}/{meta.slug}"
    )


@app.command("sync-guide")
def sync_guide(
    repo: RepoOpt = "",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """把 `kb/guide.md` 上传/更新为知识库里的《指导文档（必读）》。"""
    settings = _settings(repo, Path("workspace"), dry_run, DEFAULT_MODEL, 20)
    body = (Path(__file__).parent / "kb" / "guide.md").read_text(encoding="utf-8")
    title = GUIDE_TITLE
    with YuqueClient(
        host=settings.host, token=settings.token, repo=settings.repo, dry_run=settings.dry_run
    ) as client:
        body = _fill_notice_link(client, settings, body)
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


# ---------------------------------------------------------------- 清空测试数据

#: 代码检出的根目录 —— 服务的工作区**不该**落在它里面（`src/yuque_agent/cli.py` 往上三层）。
CHECKOUT_ROOT = Path(__file__).resolve().parents[2]


def _inside_checkout(path: Path) -> bool:
    """``path`` 是否落在代码检出里。"""
    try:
        Path(path).resolve().relative_to(CHECKOUT_ROOT)
    except ValueError:
        return False
    return True


#: 永远不碰的系统性文档（跟 `Settings.ignore_doc_titles` 一起用）。
_SYSTEM_DOC_TITLES = (GUIDE_TITLE, "指导文档", NOTICE_TITLE)


def _wipe_dir(path: Path, *, pattern: str = "*.json") -> list[str]:
    """删掉目录里的文件，返回删掉的文件名（目录不存在就啥也不做）。"""
    if not path.is_dir():
        return []
    removed = []
    for item in sorted(path.glob(pattern)):
        if item.is_file():
            item.unlink()
            removed.append(item.name)
    return removed


@app.command()
def reset_test_data(
    repo: RepoOpt = "",
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="cycle = 只清当前周期目录（默认）；all = 连归档区一起清",
        ),
    ] = "cycle",
    include_runs: Annotated[
        bool,
        typer.Option("--runs", help="连 runs/ 留痕一起删（默认保留——那是证据）"),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="真的执行；不加这个只预览")] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="工作区看起来不对（落在代码检出里）也照跑"),
    ] = False,
) -> None:
    """清空测试数据：删掉知识库里的申请文档 + 本地产出与状态，回到干净起点。

    **默认只预览**（列出会删什么），确认无误再加 `--yes`。

    ⚠️ **必须显式给对 `--workspace`**：默认值是相对路径 `workspace`，
    在检出目录里跑就会落到 `<检出>/workspace`。那是最坏的一种错——
    知识库里的申请文档**照样被删**，而本地产出清了个空
    （2026-09-25 实测踩到）。所以工作区落在代码检出里时这里会直接拒绝。

    目的是「测试完回到能交付的干净状态」：

    * 知识库：删掉周期目录（`--scope all` 再加归档区）里的**申请文档**；
      《指导文档（必读）》《Agent 通知》永远不会碰；
    * 本地产出：`outbox/applications/`、`outbox/notify/{pending,done,unrouted,failed}/`、
      审计流水与序号；
    * 状态：把删掉的文档从快照里**同步摘掉**（不删 state.json——
      否则下次启动会被当成冷启动而白跑一轮归档）。

    留痕默认保留：`runs/*/session.jsonl` 是唯一事实来源，无特殊情况不要删。
    """
    if scope not in ("cycle", "all"):
        console.print(f"[red]--scope 只能是 cycle 或 all，收到 {scope!r}[/red]")
        raise typer.Exit(2)

    settings = _settings(repo, workspace, False, DEFAULT_MODEL, 20)
    if _inside_checkout(settings.root) and not force:
        console.print(
            f"[red]工作区落在代码检出里：{settings.root}[/red]\n"
            "这几乎一定是 `--workspace` 写错了。它是最坏的一种错：\n"
            "  知识库里的申请文档**照样会被删**，而本地产出清了个空（实测踩到过）。\n"
            f"服务的工作区应当是 /var/lib/yuque-agent/workspace；确认无误要强跑加 --force。",
            markup=False,
        )
        raise typer.Exit(2)
    cycle_title = cycle_targets(clock.now())[0].title
    protected = set(_SYSTEM_DOC_TITLES) | set(settings.ignore_doc_titles)

    with YuqueClient(host=settings.host, token=settings.token, repo=settings.repo) as client:
        dir_of = doc_dir_map(client.toc())
        doomed: list[tuple[int, str, str]] = []
        for meta in client.docs():
            if meta.title in protected:
                continue
            where = dir_of.get(meta.doc_id, "")
            in_cycle = where == cycle_title
            in_archive = where == ARCHIVE_ZONE_TITLE or where.startswith(ARCHIVE_ZONE_TITLE + "/")
            if in_cycle or (scope == "all" and in_archive):
                doomed.append((meta.doc_id, meta.title, where))

        head = f"清空测试数据（scope={scope} · 当前周期 {cycle_title}）"
        if yes:
            console.print(f"[bold]{head}[/bold]")
        else:
            console.print(f"[yellow]预览（还没有删任何东西）：{head}[/yellow]")

        # 下面的行里会带 [ ] 等字面量，一律 markup=False，免得被 rich 当标签
        console.print("\n[bold]一、知识库将删除的文档[/bold]")
        if not doomed:
            console.print("  （无）", markup=False)
        for doc_id, title, where in doomed:
            console.print(f"  · {doc_id}  {title}   目录={where or '根目录'}", markup=False)
        console.print(f"  《{GUIDE_TITLE}》《{NOTICE_TITLE}》永不删。", markup=False, style="dim")

        local_plan = [
            ("outbox/applications", settings.applications_dir, "*.json"),
            ("outbox/notify/pending", settings.notify_dir / "pending", "*.json"),
            ("outbox/notify/done", settings.notify_dir / "done", "*.json"),
            ("outbox/notify/unrouted", settings.notify_dir / "unrouted", "*.json"),
            ("outbox/notify/failed", settings.notify_dir / "failed", "*.json"),
            # notes/ 是 LLM 的跨轮记忆，**格式由它自己定**（实测写过 .md）。
            # 所以这里不能只删 *.json——否则测试文档的记忆会留着，
            # 下次跑的时候 agent 会以为那些申请还在。
            ("notes", settings.notes_dir, "*"),
        ]
        console.print("\n[bold]二、本地将清空的产出[/bold]")
        for label, path, pattern in local_plan:
            n = len(list(path.glob(pattern))) if path.is_dir() else 0
            console.print(f"  · {label}: {n} 个文件", markup=False)
        console.print("  · outbox/notify/outbox.jsonl：审计流水清空、.seq 归零", markup=False)
        console.print(
            f"  · outbox/plan.json：{'会删（申请没了它就该空）' if settings.plan_file.exists() else '不存在'}",
            markup=False,
        )
        console.print(
            "  · outbox/plan.defaults.json：**保留**（借用人信息是配置，不是测试数据）",
            markup=False,
        )
        if scope == "all":
            n = len(list(settings.archive_dir.glob("*"))) if settings.archive_dir.is_dir() else 0
            console.print(
                f"  · outbox/archive/：{n} 个周期目录 —— 你选了 --scope all",
                markup=False,
                style="yellow",
            )
        else:
            console.print("  · outbox/archive/：保留（想一并清掉用 --scope all）", markup=False)
        if include_runs:
            n = len(list(settings.runs_dir.glob("*"))) if settings.runs_dir.is_dir() else 0
            console.print(
                f"  · runs/：{n} 个 —— 你加了 --runs，留痕会被删掉", markup=False, style="yellow"
            )
        console.print(
            f"  · 《{NOTICE_TITLE}》：本地通知清空后按空清单重建（只显示本周期通知）",
            markup=False,
        )

        if not yes:
            console.print("\n[bold]这只是预览。确认无误后加 --yes 真的执行。[/bold]")
            return

        removed_ids = []
        for doc_id, title, _where in doomed:
            try:
                client.delete_doc(doc_id)
                removed_ids.append(doc_id)
                console.print(f"  已删  {doc_id}  {title}", markup=False, style="green")
            except YuqueError as exc:
                console.print(f"  删除失败  {doc_id}：{exc}", markup=False, style="red")

    # ---- 本地 ----
    for _label, path, pattern in local_plan:
        _wipe_dir(path, pattern=pattern)
    (settings.notify_dir / ".seq").write_text("0", encoding="utf-8")
    audit = settings.notify_dir / "outbox.jsonl"
    if audit.exists():
        audit.write_text("", encoding="utf-8")
    outputs.rebuild_application_index(settings)
    if settings.plan_file.exists():
        settings.plan_file.unlink()
    console.print("  本地产出已清空，申请索引已重建", markup=False, style="green")

    # 本地通知清空了 → 《Agent 通知》也该是空的（它就是按本周期通知重建出来的）
    with YuqueClient(host=settings.host, token=settings.token, repo=settings.repo) as client:
        outcome = noticedoc.refresh(settings, client)
    if outcome.get("ok"):
        console.print(
            f"  《{NOTICE_TITLE}》已按空清单重建（{outcome.get('count', 0)} 条通知）",
            markup=False,
            style="green",
        )
    else:
        console.print(
            f"  《{NOTICE_TITLE}》重建失败：{outcome.get('error')}", markup=False, style="red"
        )

    if scope == "all" and settings.archive_dir.is_dir():
        shutil.rmtree(settings.archive_dir)
        console.print("  outbox/archive/ 已清空", markup=False, style="green")

    if include_runs and settings.runs_dir.is_dir():
        shutil.rmtree(settings.runs_dir)
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        console.print("  runs/ 已清空", markup=False, style="green")

    # 把删掉的文档从快照里摘掉：否则下次轮询会把它们当成「被删除」而发通知。
    # 注意：`Snapshot.docs` 的键是 **int**（见 snapshot.py），不是字符串。
    if removed_ids and settings.state_file.exists():
        state = load_state(settings.state_file)
        if state.snapshot is not None:
            for doc_id in removed_ids:
                state.snapshot.docs.pop(int(doc_id), None)
            save_state(state, settings.state_file)
        console.print(
            f"  状态快照已摘掉 {len(removed_ids)} 篇（不会因此误发「文档被删除」的通知）",
            markup=False,
            style="green",
        )

    console.print("\n[bold green]已回到干净起点。[/bold green]")


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
    if payload.get("notice_doc"):
        console.print(f"《{NOTICE_TITLE}》：{payload['notice_doc']}")
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
